#!/usr/bin/env python3
"""Tesla Wall Connector Gen 3 Neurio/Generac Modbus RTU simulator.

This program is intentionally small and explicit: a Wall Connector is the
Modbus RTU master, and this process behaves like the remote Neurio/Generac
meter the charger expects to find on its RS485 terminals.

The production setup this was built for is unusual:

* One Linux host is connected to a Moxa UPort 1650-16 USB serial adapter.
* Each Wall Connector gets its own isolated RS485 port on the Moxa adapter.
* This process opens many serial ports at once and serves the same simulated
  meter identity/current/power data to every charger.

The Wall Connector polls a very small set of holding-register ranges.  Those
ranges are documented in README.md and docs/protocol.md.  The code below keeps
the register matching deliberately literal so packet captures can be compared
line-by-line with the implementation.
"""

import argparse
import binascii
import json
import logging
import re
import signal
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import serial

# TWC Gen 3 talks to the Neurio meter at 115200 8N1 in the observed setup.
# Earlier tests at other speeds were silent; the requests below appeared
# immediately after the Moxa ports were set to RS485 2-wire mode.
BAUD = 115200

# The simulator reads current/power values from disk on demand.  That makes the
# web UI and the CLI setter cheap and robust: they only need to atomically write
# this JSON file; the serial loop notices the mtime change and uses the values
# for the next Modbus reply.
CONFIG_PATH = Path("/etc/twc-neurio-sim/values.json")
IDENTIFY_PATH = Path("/etc/twc-neurio-sim/identify.json")
ACTIVITY_PATH = Path("/run/twc-neurio-sim/port_activity.json")
NEURIO_SERIAL_PREFIX = "NEURIOMOXA"

# Logical mapping for the lab installation.  Moxa exposes physical port 1 as
# /dev/ttyMXUSB0, physical port 2 as /dev/ttyMXUSB1, and so on.
DEFAULT_PORTS = [
    (f"Moxa Port {port_number} / ttyMXUSB{port_number - 1}", f"/dev/ttyMXUSB{port_number - 1}")
    for port_number in range(1, 17)
]

# Identity response template copied from the known-working Neurio response corpus in
# frankenbubble/twc3-modbus.  It contains a model/firmware-ish string, a meter
# id ("90954"), a serial number field, and a MAC-like string.
# The Wall Connector uses this during "Remote Meter" discovery/configuration.
IDENTITY_REGS = [
    0x3078, 0x3030, 0x3030, 0x3034, 0x3731, 0x3442, 0x3035, 0x3638,
    0x3631, 0x0000, 0x312E, 0x362E, 0x312D, 0x5465, 0x736C, 0x6100,
    0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0x3031, 0x322E, 0x3030, 0x3032,
    0x3041, 0x2E48, 0x0000, 0xFFFF, 0x3930, 0x3935, 0x3400, 0x5641,
    0x4834, 0x3831, 0x3041, 0x4230, 0x3233, 0x3100, 0xFFFF, 0xFFFF,
    0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0x3034, 0x3A37,
    0x313A, 0x3442, 0x3A30, 0x353A, 0x3638, 0x3A36, 0x3100,
]

# Register 0x9C42 ("40002" in the source response corpus) appears to be a
# discovery/handshake read.  Words 2-5 decode as ASCII "Generac\0".
HANDSHAKE_REGS = [0x0001, 0x0042, 0x4765, 0x6E65, 0x7261, 0x6300]

# Fallback values are only used if /etc/twc-neurio-sim/values.json is absent or
# malformed.  They match example response values from twc3-modbus and are not
# intended as a safe load-balancing target.
DEFAULT_POWER_FLOATS = [87.2903, 378.2907, 2.1172, -0.1267, -46.0664]
DEFAULT_CURRENT_FLOATS = [1.0979, 3.7791, 0.4527, 0.0245]

_last_config_mtime = None
_cached_values = None
_last_identify_mtime = None
_cached_identify = None
_last_activity_write = 0.0


def text_to_regs(text: str, register_count: int) -> list[int]:
    """Encode an ASCII-ish identity field into fixed-width Modbus registers."""
    raw = text.encode("ascii")[: register_count * 2]
    raw = raw.ljust(register_count * 2, b"\x00")
    return [(raw[i] << 8) | raw[i + 1] for i in range(0, len(raw), 2)]


def identity_serial_for_port(port_number: int) -> str:
    """Return the unique simulated Neurio serial for one Moxa physical port."""
    return f"{NEURIO_SERIAL_PREFIX}{port_number:03d}"


def identity_regs_for_port(port_number: int) -> list[int]:
    """Return identity registers with the per-port Neurio serial patched in.

    The original accepted identity block has a seven-register serial field at
    indexes 31..37.  `NEURIOMOXA001` is exactly 13 characters, matching the
    original VAH4810AB0231 field length with a trailing NUL byte.
    """
    regs = list(IDENTITY_REGS)
    regs[31:38] = text_to_regs(identity_serial_for_port(port_number), 7)
    return regs


def floats_to_regs(values: list[float]) -> list[int]:
    """Convert IEEE-754 float32 values to Modbus register words.

    The Wall Connector expects big-endian float payloads split into 16-bit
    registers.  Modbus RTU itself appends a little-endian CRC at the end of the
    whole frame, so byte order is easy to confuse.  For a float, we use:

        15.0 A -> 0x41700000 -> registers [0x4170, 0x0000]
    """
    regs = []
    for value in values:
        raw = struct.pack(">f", float(value))
        regs.append((raw[0] << 8) | raw[1])
        regs.append((raw[2] << 8) | raw[3])
    return regs


def load_values() -> tuple[list[int], list[int]]:
    """Load simulated power/current data and return register words.

    values.json schema:

    * power_w: five float values => ten Modbus registers for address 0x0088.
    * current_a: four float values => eight Modbus registers for address 0x00F4.

    The first three current values are L1/L2/L3.  The fourth value is total
    current.  That ordering was confirmed visually in Tesla One by setting
    10/20/30 A and seeing the phases appear in the same order.
    """
    global _last_config_mtime, _cached_values
    try:
        stat = CONFIG_PATH.stat()
    except FileNotFoundError:
        return floats_to_regs(DEFAULT_POWER_FLOATS), floats_to_regs(DEFAULT_CURRENT_FLOATS)

    if _cached_values is not None and _last_config_mtime == stat.st_mtime_ns:
        return _cached_values

    try:
        data = json.loads(CONFIG_PATH.read_text())
        power = data.get("power_w", DEFAULT_POWER_FLOATS)
        current = data.get("current_a", DEFAULT_CURRENT_FLOATS)
        if len(power) != 5:
            raise ValueError("power_w must contain 5 floats")
        if len(current) != 4:
            raise ValueError("current_a must contain 4 floats")
        _cached_values = (floats_to_regs(power), floats_to_regs(current))
        _last_config_mtime = stat.st_mtime_ns
        logging.info("Loaded values: power_w=%s current_a=%s", power, current)
        return _cached_values
    except Exception as exc:
        logging.warning("Could not load %s: %s; using defaults", CONFIG_PATH, exc)
        return floats_to_regs(DEFAULT_POWER_FLOATS), floats_to_regs(DEFAULT_CURRENT_FLOATS)


def load_identify_overrides() -> dict[int, tuple[list[int], list[int]]]:
    """Load short-lived per-port meter values for port identification.

    identify.json is intentionally separate from values.json so the normal
    load-balancing values remain untouched.  The web service can write entries
    like:

        {"ports": {"8": {"expires_at": 1234567890.0,
                         "current_a": [8.08, 8.08, 8.08, 24.24],
                         "power_w": [...]}}}

    Expired or malformed entries are ignored.
    """
    global _last_identify_mtime, _cached_identify
    try:
        stat = IDENTIFY_PATH.stat()
    except FileNotFoundError:
        return {}

    if _cached_identify is not None and _last_identify_mtime == stat.st_mtime_ns:
        return _cached_identify

    overrides = {}
    now = time.time()
    try:
        data = json.loads(IDENTIFY_PATH.read_text())
        for raw_port, item in (data.get("ports") or {}).items():
            port_number = int(raw_port)
            expires_at = float(item.get("expires_at", 0))
            current = item.get("current_a", [])
            power = item.get("power_w", [])
            if expires_at <= now or len(current) != 4 or len(power) != 5:
                continue
            overrides[port_number] = (floats_to_regs(power), floats_to_regs(current))
    except Exception as exc:
        logging.warning("Could not load %s: %s; ignoring identify overrides", IDENTIFY_PATH, exc)
        overrides = {}

    _cached_identify = overrides
    _last_identify_mtime = stat.st_mtime_ns
    return overrides


def load_values_for_port(port_number: int) -> tuple[list[int], list[int]]:
    identify = load_identify_overrides()
    if port_number in identify:
        return identify[port_number]
    return load_values()


def crc16_modbus(data: bytes) -> bytes:
    """Return Modbus RTU CRC16 as two little-endian bytes."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc.to_bytes(2, "little")


def valid_crc(frame: bytes) -> bool:
    return len(frame) >= 4 and crc16_modbus(frame[:-2]) == frame[-2:]


def find_modbus_request(buffer: bytearray) -> int | None:
    """Return the offset of the next valid 8-byte Wall Connector request.

    Serial reads are byte streams, not message queues.  In practice a Wall
    Connector reset can leave a Moxa port buffer shifted by one byte:

        expected: 01 03 00 00 00 01 84 0a
        shifted:  03 00 00 00 01 84 0a 01

    If we always consume the first eight bytes, that port can stay permanently
    out of phase and never answer discovery.  Instead, each port scans its own
    buffer for slave id 1, function 3, and a valid Modbus CRC.
    """
    for offset in range(0, len(buffer) - 7):
        frame = bytes(buffer[offset : offset + 8])
        if frame[0] == 1 and frame[1] == 3 and valid_crc(frame):
            return offset
    return None


def response(slave: int, func: int, regs: list[int]) -> bytes:
    """Build a Modbus function-3 response frame from 16-bit register words."""
    body = bytes([slave, func, len(regs) * 2])
    for reg in regs:
        body += bytes([(reg >> 8) & 0xFF, reg & 0xFF])
    return body + crc16_modbus(body)


def make_reply(frame: bytes, port_number: int, identity_regs: list[int]) -> bytes | None:
    """Return the simulator reply for one 8-byte Modbus RTU request.

    The Wall Connector requests observed so far are all function-code 3
    ("Read Holding Registers") with slave id 1 and fixed 8-byte request frames.
    Unknown requests are ignored rather than answered with a Modbus exception;
    this mirrors the behavior we used during bring-up and makes mistakes noisy
    in logs without sending surprising bytes on the RS485 bus.
    """
    if len(frame) != 8 or not valid_crc(frame):
        return None

    slave, func = frame[0], frame[1]
    addr = (frame[2] << 8) | frame[3]
    count = (frame[4] << 8) | frame[5]

    if slave != 1 or func != 3:
        return None
    if addr == 0x9C42 and count == 6:
        return response(slave, func, HANDSHAKE_REGS)
    if addr == 0x0001 and count == 55:
        return response(slave, func, identity_regs)
    if addr == 0x0000 and count == 1:
        return response(slave, func, [identity_regs[0]])

    power_regs, current_regs = load_values_for_port(port_number)
    if addr == 0x0088 and count == 10:
        return response(slave, func, power_regs)
    if addr == 0x00F4 and count == 8:
        return response(slave, func, current_regs)
    return None


@dataclass
class PortState:
    label: str
    path: str
    port_number: int
    identity_serial: str
    identity_regs: list[int]
    ser: serial.Serial
    buffer: bytearray
    request_count: int = 0
    last_request_at: float | None = None
    last_addr: int | None = None
    last_count: int | None = None
    last_rx_hex: str | None = None
    request_counts_by_addr: dict[str, int] | None = None
    identity_read_count: int = 0
    last_identity_at: float | None = None
    last_identity_serial: str | None = None


def port_number_from_path(path: str, fallback: int) -> int:
    match = re.search(r"ttyMXUSB(\d+)$", path)
    if match:
        return int(match.group(1)) + 1
    return fallback


def open_ports(port_specs: list[str]) -> list[PortState]:
    """Open every configured serial device in non-blocking mode."""
    ports = []
    for index, spec in enumerate(port_specs, start=1):
        if "=" in spec:
            label, path = spec.split("=", 1)
        else:
            path = spec
            label = path.rsplit("/", 1)[-1]
        port_number = port_number_from_path(path, index)
        identity_serial = identity_serial_for_port(port_number)
        ser = serial.Serial(path, BAUD, bytesize=8, parity="N", stopbits=1, timeout=0)
        ports.append(PortState(
            label=label,
            path=path,
            port_number=port_number,
            identity_serial=identity_serial,
            identity_regs=identity_regs_for_port(port_number),
            ser=ser,
            buffer=bytearray(),
        ))
        logging.info("Listening/responding on %s (%s) @ %s 8N1 as %s", label, path, BAUD, identity_serial)
    return ports


def mark_activity(state: PortState, frame: bytes) -> None:
    state.request_count += 1
    state.last_request_at = time.time()
    state.last_addr = (frame[2] << 8) | frame[3]
    state.last_count = (frame[4] << 8) | frame[5]
    state.last_rx_hex = binascii.hexlify(frame, " ").decode()
    if state.request_counts_by_addr is None:
        state.request_counts_by_addr = {}
    addr_key = f"0x{state.last_addr:04x}"
    state.request_counts_by_addr[addr_key] = state.request_counts_by_addr.get(addr_key, 0) + 1
    if state.last_addr == 0x0001 and state.last_count == 55:
        state.identity_read_count += 1
        state.last_identity_at = state.last_request_at
        state.last_identity_serial = state.identity_serial


def write_activity(ports: list[PortState], force: bool = False) -> None:
    """Write lightweight per-port activity for the web UI.

    This is intentionally best-effort.  Serial replies must keep working even if
    `/run` is temporarily unavailable.
    """
    global _last_activity_write
    now = time.time()
    if not force and now - _last_activity_write < 0.5:
        return
    _last_activity_write = now
    payload = {
        "updated_at": now,
        "ports": [
            {
                "moxa_port": state.port_number,
                "tty": state.path,
                "identity_serial": state.identity_serial,
                "request_count": state.request_count,
                "last_request_at": state.last_request_at,
                "last_addr": state.last_addr,
                "last_count": state.last_count,
                "last_rx_hex": state.last_rx_hex,
                "request_counts_by_addr": state.request_counts_by_addr or {},
                "identity_read_count": state.identity_read_count,
                "last_identity_at": state.last_identity_at,
                "last_identity_serial": state.last_identity_serial,
            }
            for state in sorted(ports, key=lambda item: item.port_number)
        ],
    }
    try:
        ACTIVITY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = ACTIVITY_PATH.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
        tmp_path.replace(ACTIVITY_PATH)
    except Exception as exc:
        logging.debug("Could not write %s: %s", ACTIVITY_PATH, exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Tesla Wall Connector Gen3 Neurio Modbus RTU simulator")
    parser.add_argument("--port", action="append", help="Port spec, e.g. 'WC1=/dev/ttyMXUSB0'. May be repeated.")
    parser.add_argument("--quiet", action="store_true", help="Only log unknown frames and config reloads/startup/shutdown.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port_specs = args.port or [f"{label}={path}" for label, path in DEFAULT_PORTS]
    ports = open_ports(port_specs)
    write_activity(ports, force=True)
    running = True

    def stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        while running:
            idle = True
            for state in ports:
                # Non-blocking read: each port maintains its own buffer because
                # multiple Wall Connectors can poll at nearly the same time.
                data = state.ser.read(256)
                if not data:
                    continue
                idle = False
                state.buffer.extend(data)

                while len(state.buffer) >= 8:
                    # Every request we have observed is exactly 8 bytes:
                    # slave, function, address hi/lo, count hi/lo, CRC lo/hi.
                    # Still, reads can begin mid-frame after reset/noise, so
                    # recover by scanning for the next CRC-valid request.
                    offset = find_modbus_request(state.buffer)
                    if offset is None:
                        if len(state.buffer) > 64:
                            discarded = bytes(state.buffer[:-7])
                            del state.buffer[:-7]
                            logging.warning(
                                "%s discarded unsynced bytes: %s",
                                state.label,
                                binascii.hexlify(discarded, " ").decode(),
                            )
                        break

                    if offset:
                        discarded = bytes(state.buffer[:offset])
                        del state.buffer[:offset]
                        logging.warning(
                            "%s resynced after dropping %d byte(s): %s",
                            state.label,
                            offset,
                            binascii.hexlify(discarded, " ").decode(),
                        )

                    frame = bytes(state.buffer[:8])
                    del state.buffer[:8]
                    reply = make_reply(frame, state.port_number, state.identity_regs)
                    frame_hex = binascii.hexlify(frame, " ").decode()

                    if reply:
                        mark_activity(state, frame)
                        write_activity(ports)
                        state.ser.write(reply)
                        state.ser.flush()
                        if not args.quiet:
                            reply_hex = binascii.hexlify(reply, " ").decode()
                            logging.info("%s RX %s", state.label, frame_hex)
                            logging.info("%s TX %s", state.label, reply_hex)
                    else:
                        logging.warning("%s unknown/invalid request: %s", state.label, frame_hex)
            if idle:
                time.sleep(0.005)
    finally:
        for state in ports:
            state.ser.close()
        logging.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
