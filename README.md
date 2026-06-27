# TWC Neurio Moxa Bridge

Linux-based Neurio/Generac meter simulator for Tesla Wall Connector Gen 3 using
a Moxa UPort 1650-16 multi-port USB RS485 adapter.

The project goal is to make many Wall Connectors believe they each have their
own Neurio remote meter attached, even though the load-balancing logic is
centralized on one small Linux host.

> Status: field prototype.  It has been tested with two Tesla Wall Connector
> Gen 3 units, a Moxa UPort 1650-16, Debian 13 and Wall Connector firmware
> `26.18.0+g114f7602e40a09`.

## Why This Exists

Tesla Wall Connector Gen 3 supports external metering with a Neurio/Generac
meter over RS485.  The practical limitation is that a physical meter normally
connects to one charger.

This project uses a Moxa multi-port adapter to isolate every charger on its own
RS485 port:

```text
                 USB
Linux host  <---------->  Moxa UPort 1650-16
                              | port 1 / ttyMXUSB0 -> Wall Connector 1
                              | port 2 / ttyMXUSB1 -> Wall Connector 2
                              | port 3 / ttyMXUSB2 -> Wall Connector 3
                              | ...
```

The Linux process answers the Modbus RTU requests from every Wall Connector and
serves the same simulated Neurio identity/current/power values to each one.

## Components

| Component | Path | Purpose |
| --- | --- | --- |
| Serial simulator | `src/twc_neurio_sim/twc_neurio_sim.py` | Modbus RTU slave that answers Wall Connector meter requests |
| CLI value setter | `src/twc_neurio_sim/set_neurio_values.py` | Writes manual L1/L2/L3 current values to `values.json` |
| Web UI | `src/twc_neurio_sim/web/server.py` | Plain HTTP control/status page |
| Simulator service | `systemd/twc-neurio-sim.service` | Starts Moxa driver, configures RS485 2-wire, runs simulator |
| Web service | `systemd/twc-neurio-web.service` | Runs the local web UI |
| Example values | `examples/values.json` | Example simulated Neurio current/power |
| Protocol docs | `docs/protocol.md` | Register-by-register Modbus documentation |
| Moxa docs | `docs/moxa.md` | Driver, port mapping and `setserial` notes |
| Wall Connector API docs | `docs/wall-connector-api.md` | Local HTTP API fields used by the UI |

## Current Features

- Simulates a Neurio/Generac meter over Modbus RTU.
- Handles multiple independent RS485 ports at the same time.
- Responds to the observed Tesla Wall Connector Gen 3 register reads:
  - handshake
  - identity
  - power floats
  - current floats
- Lets the operator manually set simulated L1/L2/L3 current values.
- Provides a local web UI on port `8080`.
- Scans the local subnet for Wall Connector HTTP APIs.
- Shows Wall Connector online/offline/charging status.
- Shows live Neurio values and browser-side last-hour graphs.
- Opens all 16 Moxa UPort 1650-16 serial ports by default.
- Broadcasts a unique simulated Neurio serial per Moxa port:
  `NEURIOMOXA001` through `NEURIOMOXA016`.
- Shows recent Modbus/RS485 activity per port in the web UI.
- Shows each Moxa port's current interface configuration from `setserial -g`
  (`RS232`, `RS485 2-wire`, `RS422`, or `RS485 4-wire`).
- Lets the operator save a human-friendly Wall Connector name on the charger
  serial number, not on the Moxa port.
- Can use a Fronius Smart Meter as an impromptu current source for the
  simulated Neurio values.

## What It Does Not Do Yet

- It does not yet implement a production automatic load-balancing algorithm.
- It does not persist Neurio history beyond the browser session.
- It does not authenticate the web UI.
- It does not guarantee safe operation on all Tesla firmware versions.

The current `Auto` mode selector in the web UI is a placeholder for the next
control-loop step.  Manual mode and Fronius mode are currently meaningful.

## Safety Warning

This project can influence EV charging behavior by presenting synthetic load
values to Wall Connectors.  Treat it as experimental until you have validated it
with your own electrical installation, charger firmware, protective devices and
local regulations.

Do not use this as the only safety mechanism protecting a main fuse or feeder.

## Protocol Summary

See [docs/protocol.md](docs/protocol.md) for the full detail.

| Address | Count | Data | Source in code |
| --- | ---: | --- | --- |
| `0x9C42` | 6 | Handshake / `Generac` response | `HANDSHAKE_REGS` |
| `0x0001` | 55 | Identity block with serial `VAH4810AB0231` | `IDENTITY_REGS` |
| `0x0000` | 1 | First identity word | `IDENTITY_REGS[0]` |
| `0x0088` | 10 | Five float32 power values | `power_w` |
| `0x00F4` | 8 | Four float32 current values | `current_a` |

Serial layer:

```text
RS485 2-wire, 115200 baud, 8N1, Modbus RTU, slave id 1
```

Confirmed current ordering:

```text
current_a = [L1, L2, L3, total]
```

That ordering was confirmed in Tesla One by setting:

```bash
sudo /opt/twc-neurio-sim/set_neurio_values.py 10 20 30
```

and observing:

```text
Phase 1 = 10 A
Phase 2 = 20 A
Phase 3 = 30 A
```

## Moxa Requirements

See [docs/moxa.md](docs/moxa.md) for the full detail.

The most important Moxa-specific requirement is RS485 2-wire mode:

```bash
for i in $(seq 0 15); do
  setserial /dev/ttyMXUSB$i port 0x1
done
```

Verify:

```bash
setserial -g /dev/ttyMXUSB0 /dev/ttyMXUSB1 /dev/ttyMXUSB15
```

Expected:

```text
/dev/ttyMXUSB0, UART: 16550A, Port: 0x0001, IRQ: 0, Flags: low_latency
/dev/ttyMXUSB1, UART: 16550A, Port: 0x0001, IRQ: 0, Flags: low_latency
/dev/ttyMXUSB15, UART: 16550A, Port: 0x0001, IRQ: 0, Flags: low_latency
```

If the port shows `Port: 0x0000`, it is still RS232 mode and the Wall Connector
traffic will look dead.

## Installation on Debian

The tested host used Debian 13 and Python 3.13.

### 1. Install OS packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip setserial curl jq git
```

Install and load the Moxa UPort Linux driver separately.  The tested Moxa driver
was the Kernel 6.x v6.2 driver from Moxa.

### 2. Install project files

Recommended target layout:

```text
/opt/twc-neurio-sim/
/etc/twc-neurio-sim/
```

Example:

```bash
sudo mkdir -p /opt/twc-neurio-sim /etc/twc-neurio-sim
sudo cp -a src/twc_neurio_sim/twc_neurio_sim.py /opt/twc-neurio-sim/
sudo cp -a src/twc_neurio_sim/set_neurio_values.py /opt/twc-neurio-sim/
sudo mkdir -p /opt/twc-neurio-sim/web
sudo cp -a src/twc_neurio_sim/web/server.py /opt/twc-neurio-sim/web/
sudo cp -a examples/values.json /etc/twc-neurio-sim/values.json
sudo cp -a examples/known_wall_connectors.json /etc/twc-neurio-sim/known_wall_connectors.json
sudo cp -a examples/fronius.json /etc/twc-neurio-sim/fronius.json
sudo chmod +x /opt/twc-neurio-sim/*.py /opt/twc-neurio-sim/web/server.py
```

Install Python dependency:

```bash
sudo python3 -m pip install pyserial --break-system-packages
```

If you prefer a venv, adjust the systemd unit `ExecStart` paths accordingly.

### 3. Install systemd units

```bash
sudo cp systemd/twc-neurio-sim.service /etc/systemd/system/
sudo cp systemd/twc-neurio-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable twc-neurio-sim.service twc-neurio-web.service
sudo systemctl start twc-neurio-sim.service twc-neurio-web.service
```

### 4. Check status

```bash
systemctl status twc-neurio-sim.service
systemctl status twc-neurio-web.service
```

Open:

```text
http://<linux-host-ip>:8080
```

For example, if the Linux host is `192.0.2.10`, open
`http://192.0.2.10:8080`.

## Manual Testing

Set simulated current:

```bash
sudo /opt/twc-neurio-sim/set_neurio_values.py 5 5 5
sudo /opt/twc-neurio-sim/set_neurio_values.py 10 20 30
sudo /opt/twc-neurio-sim/set_neurio_values.py 15 15 15
```

Read current web API values:

```bash
curl -s http://127.0.0.1:8080/api/neurio | jq
curl -s http://127.0.0.1:8080/api/devices | jq
curl -s http://127.0.0.1:8080/api/fronius | jq
```

Watch serial simulator logs:

```bash
journalctl -u twc-neurio-sim.service -f
```

## Adding More Wall Connectors

For each additional charger:

1. Wire its RS485 pair to a free Moxa port.
2. Make sure that Moxa port is RS485 2-wire:

   ```bash
   setserial /dev/ttyMXUSB2 port 0x1
   ```

3. Use the web UI scan to discover the charger's Wi-Fi API.
4. If needed, edit `/etc/twc-neurio-sim/known_wall_connectors.json` and set
   `moxa_port` so the UI shows the charger on the correct physical port.

The default systemd unit and simulator already cover all 16 ports on a
UPort 1650-16.  Manual `--port` arguments are only needed for unusual adapters
or custom device names.

Example manual run:

```bash
sudo /opt/twc-neurio-sim/twc_neurio_sim.py \
  --port 'WC1=/dev/ttyMXUSB0' \
  --port 'WC2=/dev/ttyMXUSB1' \
  --port 'WC3=/dev/ttyMXUSB2'
```

## Fronius Smart Meter Mode

The web UI can use a Fronius inverter's open Solar API as an impromptu Neurio
meter source.  This is useful during development when a physical Neurio meter is
not installed but a Fronius Smart Meter is already measuring site current.

In the web UI:

1. Open the Fronius Smart Meter panel.
2. Enter the Fronius inverter IP address.
3. Enable `Aktiv`.
4. Select `Fronius` as the Neurio mode.

The server calls:

```text
http://<fronius-ip>/solar_api/v1/GetMeterRealtimeData.cgi?Scope=System
```

and writes the returned phase currents/powers to
`/etc/twc-neurio-sim/values.json`.  The serial simulator then serves those
values on every Moxa port on the next Wall Connector poll.

Configuration is stored locally in:

```text
/etc/twc-neurio-sim/fronius.json
```

Example shape:

```json
{
  "enabled": true,
  "ip": "192.0.2.20",
  "updated_at": "2026-06-27T14:00:00+02:00"
}
```

Important limitation: depending on meter placement, the Fronius Smart Meter may
include Wall Connector charging current in the measured site load.  Feeding that
raw value back into the Wall Connector can create a hunting loop where charging
starts, the measured load rises, and the charger stops again.  Treat Fronius
mode as a development/debug source until your control algorithm subtracts known
charger current or otherwise filters the value safely.

## Observed Wall Connector Behavior

With the Wall Connector configured for 16 A max:

| Simulated Neurio current | Observed behavior |
| --- | --- |
| `5 / 5 / 5 A` | Charging started/restarted |
| `16 / 16 / 17 A` | Vehicle max current dropped to about 15 A |
| `16 / 16 / 20 A` | Charging stopped |

Observed hysteresis:

- minimum charging current appears to be 5 A
- roughly 6 A headroom needed to restart
- roughly 2 A over-current can stop charging

These are field observations, not guaranteed rules.

## Web UI

The web UI is deliberately simple:

- Python standard library only.
- Plain HTTP.
- No login.
- Designed for a trusted local network.

Endpoints:

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/` | GET | UI |
| `/api/status` | GET | Server status |
| `/api/scan` | GET | Scan subnet for Wall Connectors |
| `/api/devices` | GET | Poll known Wall Connectors |
| `/api/neurio` | GET | Read current simulated values |
| `/api/neurio` | POST | Write manual simulated values |
| `/api/fronius` | GET | Read Fronius integration status/live meter values |
| `/api/fronius` | POST | Configure Fronius Smart Meter integration |
| `/api/device-name` | POST | Save a human-friendly Wall Connector name |

The web UI reads `/run/twc-neurio-sim/port_activity.json`, which is written by
the serial simulator.  This lets the UI show which physical Moxa ports are
currently being polled over RS485 even before a Wall Connector has been mapped
to that port.

## Credits

This project stands on prior work:

- https://github.com/Klangen82/tesla-wall-connector-control
- https://github.com/frankenbubble/twc3-modbus

See [NOTICE.md](NOTICE.md) for details.

## License

GPL-3.0-or-later.  See [LICENSE](LICENSE).

The GPL license is used because this repository includes protocol response data
derived from `frankenbubble/twc3-modbus`, which is GPL-3.0.
