# Protocol Documentation

This document describes the observed Tesla Wall Connector Gen 3 remote-meter
protocol used by this project.

The protocol is not official Tesla documentation.  It is based on:

- Known-good response data from `frankenbubble/twc3-modbus`.
- Prior work in `Klangen82/tesla-wall-connector-control`.
- Live testing with Tesla Wall Connector Gen 3 firmware `26.18.0`.
- Tesla One observations while configuring a remote `Neurio Meter`.

## Physical and Serial Layer

| Layer | Value |
| --- | --- |
| Bus | RS485 |
| Wiring | 2-wire half duplex |
| Baud | 115200 |
| Framing | 8 data bits, no parity, 1 stop bit (`8N1`) |
| Master | Tesla Wall Connector Gen 3 |
| Slave | Simulated Neurio/Generac meter |
| Slave id | `1` |
| Function used | `0x03` Read Holding Registers |

The Wall Connector sends 8-byte Modbus RTU request frames:

```text
slave function address_hi address_lo count_hi count_lo crc_lo crc_hi
```

This simulator currently answers only exact request shapes that have been
observed in the field.  Unknown or invalid frames are logged and ignored.

## CRC

Frames use standard Modbus RTU CRC16:

- Polynomial: `0xA001`
- Initial value: `0xFFFF`
- Appended little-endian: low byte first, high byte second

Example observed request:

```text
01 03 9c 42 00 06 4b 8c
```

Meaning:

| Byte(s) | Value | Meaning |
| --- | --- | --- |
| `01` | `0x01` | Slave id |
| `03` | `0x03` | Read Holding Registers |
| `9c 42` | `0x9C42` | Start register |
| `00 06` | `6` | Register count |
| `4b 8c` | CRC | Modbus CRC16, little-endian |

## Register Summary

| Start address | Decimal | Count | Purpose | Implemented by |
| --- | ---: | ---: | --- | --- |
| `0x9C42` | `40002` | 6 | Handshake / manufacturer probe | Static registers |
| `0x0001` | `1` | 55 | Identity block | Static registers |
| `0x0000` | `0` | 1 | First identity word probe | Static register |
| `0x0088` | `136` | 10 | Power block, 5 float32 values | Dynamic `power_w` |
| `0x00F4` | `244` | 8 | Current block, 4 float32 values | Dynamic `current_a` |

## Response Frame Format

All implemented responses use Modbus function `0x03`:

```text
slave function byte_count payload... crc_lo crc_hi
```

For example, a six-register response has `byte_count = 12`.

## Register `0x9C42`, Count 6: Handshake

Observed request:

```text
01 03 9c 42 00 06 4b 8c
```

Static response registers:

```text
0x0001
0x0042
0x4765
0x6E65
0x7261
0x6300
```

ASCII interpretation:

```text
0x4765 0x6E65 0x7261 0x6300 -> "Generac\0"
```

This appears to be a discovery or compatibility probe.  `frankenbubble/twc3-modbus`
stores the same payload in `responses/40002`.

## Register `0x0001`, Count 55: Identity

Observed request:

```text
01 03 00 01 00 37 55 d4
```

Response contains 55 Modbus registers.  The simulator starts from this template:

```text
0x3078 0x3030 0x3030 0x3034 0x3731 0x3442 0x3035 0x3638
0x3631 0x0000 0x312E 0x362E 0x312D 0x5465 0x736C 0x6100
0xFFFF 0xFFFF 0xFFFF 0xFFFF 0x3031 0x322E 0x3030 0x3032
0x3041 0x2E48 0x0000 0xFFFF 0x3930 0x3935 0x3400 0x5641
0x4834 0x3831 0x3041 0x4230 0x3233 0x3100 0xFFFF 0xFFFF
0xFFFF 0xFFFF 0xFFFF 0xFFFF 0xFFFF 0xFFFF 0x3034 0x3A37
0x313A 0x3442 0x3A30 0x353A 0x3638 0x3A36 0x3100
```

Human-readable fields visible in this block:

| Register range | ASCII-ish value | Notes |
| --- | --- | --- |
| First words | `0x0004714B056861`-like string | Exact semantic unknown |
| Mid block | `1.6.1-Tesla` | Firmware/vendor-ish string |
| Mid block | `012.0020A.H` | Exact semantic unknown |
| Mid block | `90954` | Meter id shown in Tesla One |
| Serial block | `VAH4810AB0231` in the original template | Patched per Moxa port by the simulator |
| Tail block | `04:71:4B:05:68:61` | MAC-like identifier |

Important: Tesla One showed the original simulated remote meter serial number as:

```text
VAH4810AB0231
```

That confirmed this identity response is accepted by Wall Connector firmware
`26.18.0`.

For multi-port operation the simulator now patches the serial field per physical
Moxa port:

| Moxa port | Simulated Neurio serial |
| ---: | --- |
| 1 | `NEUROMOXA_001` |
| 2 | `NEUROMOXA_002` |
| ... | ... |
| 16 | `NEUROMOXA_016` |

This gives each isolated RS485 link a distinct meter identity and makes future
port-to-Wall-Connector autodetection possible if the Wall Connector exposes the
configured remote-meter identity through any API or observable behavior.

## Register `0x0000`, Count 1: Identity Probe

Observed request:

```text
01 03 00 00 00 01 84 0a
```

The simulator returns the first word of the identity block:

```text
0x3078
```

This is likely a lightweight presence or compatibility check.

## Register `0x0088`, Count 10: Power Values

Observed request:

```text
01 03 00 88 00 0a 45 e7
```

The response is ten registers, interpreted as five big-endian IEEE-754 float32
values.

The simulator maps these values to `power_w` in `/etc/twc-neurio-sim/values.json`:

```json
{
  "power_w": [3450.0, 3450.0, 3450.0, 10350.0, 0.0]
}
```

Current meaning used by this project:

| Float index | Meaning | Example |
| ---: | --- | ---: |
| 0 | L1 power in watts | `3450.0` |
| 1 | L2 power in watts | `3450.0` |
| 2 | L3 power in watts | `3450.0` |
| 3 | Total three-phase power in watts | `10350.0` |
| 4 | Unknown / unused signed value | `0.0` |

The original `frankenbubble/twc3-modbus` response for register `136` contains:

```text
0x42AE 0x94A0
0x43BD 0x2517
0x4007 0x805A
0xBE01 0xC3BF
0xC238 0x430D
```

Those decode to approximately:

```text
[87.2903, 378.2907, 2.1172, -0.1267, -46.0664]
```

## Register `0x00F4`, Count 8: Current Values

Observed request:

```text
01 03 00 f4 00 08 05 fe
```

The response is eight registers, interpreted as four big-endian IEEE-754 float32
values.

The simulator maps these values to `current_a` in `/etc/twc-neurio-sim/values.json`:

```json
{
  "current_a": [15.0, 15.0, 15.0, 45.0]
}
```

Confirmed ordering:

| Float index | Meaning | Evidence |
| ---: | --- | --- |
| 0 | L1 current in amperes | Tesla One showed Phase 1 = 10 A when set to `10 20 30` |
| 1 | L2 current in amperes | Tesla One showed Phase 2 = 20 A |
| 2 | L3 current in amperes | Tesla One showed Phase 3 = 30 A |
| 3 | Total current in amperes | Sum of L1/L2/L3 |

The original `frankenbubble/twc3-modbus` response for register `244` contains:

```text
0x3F8C 0x888D
0x4071 0xDD46
0x3EE7 0xCB0D
0x3CC8 0x5BB6
```

Those decode to approximately:

```text
[1.0979, 3.7791, 0.4527, 0.0245]
```

## Field Observations: Wall Connector Load Behavior

Test setup:

- Wall Connector max / main fuse setting: 16 A.
- Simulated Neurio current changed manually.
- Vehicle connected and charging.

Observed behavior:

| Simulated house current | Observed Wall Connector behavior |
| --- | --- |
| `5 / 5 / 5 A` | Charging starts/restarts automatically |
| `11 / 11 / 11 A` | Still charges near 16 A |
| `14 / 14 / 14 A` | Still charges near 16 A |
| `15 / 15 / 15 A` | Still charges near 16 A |
| `16 / 16 / 17 A` | Tesla max charge rate dropped to about 15 A |
| `16 / 16 / 20 A` | Charging stopped due to over-current condition |
| back to `5 / 5 / 5 A` | Charging restarted automatically |

Hysteresis observed in the field:

- Minimum charging current appears to be 5 A.
- About 6 A of headroom appears to be required before charging restarts.
- About 2 A of over-current can be enough to force stop.

These observations are not a formal control algorithm.  They are included so
future automatic load-balancing work can avoid aggressive oscillation.

## Firmware Note

`Klangen82/tesla-wall-connector-control` documents that firmware `26.2+`
changed active charging behavior for that ESPHome-based implementation.  In our
field test on firmware:

```text
26.18.0+g114f7602e40a09
```

the Wall Connector still discovered the remote meter and reacted to simulated
current values in at least some states.  The precise firmware-dependent behavior
needs more testing before this should be considered production-safe.
