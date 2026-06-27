# Wall Connector HTTP API Notes

Tesla Wall Connector Gen 3 exposes an unauthenticated local HTTP API on the
LAN.  This project uses it only for discovery and status display; it does not
use it to control charging.

## Endpoints Used

| Endpoint | Purpose |
| --- | --- |
| `/api/1/vitals` | Detect a Wall Connector and read live charging status |
| `/api/1/version` | Read serial number and firmware version |

## Discovery Logic

The web UI scans a subnet and probes each host:

1. Request `http://<ip>/api/1/vitals`.
2. Check that JSON is returned.
3. Check that the response contains `evse_state`.
4. If that passes, request `/api/1/version` for identity metadata.

This is intentionally conservative so random web servers are not shown as
chargers.

## Observed Fields from `/api/1/vitals`

| Field | Meaning |
| --- | --- |
| `contactor_closed` | Whether the Wall Connector contactor is closed |
| `vehicle_connected` | Whether a vehicle is plugged in |
| `vehicle_current_a` | Vehicle current in amperes |
| `currentA_a` | Phase A current |
| `currentB_a` | Phase B current |
| `currentC_a` | Phase C current |
| `currentN_a` | Neutral current |
| `voltageA_v` | Phase A voltage |
| `voltageB_v` | Phase B voltage |
| `voltageC_v` | Phase C voltage |
| `session_energy_wh` | Energy in current session |
| `evse_state` | EVSE state integer |
| `current_alerts` | Active alerts |
| `evse_not_ready_reasons` | Reasons charging is not ready |

Example from the field:

```json
{
  "contactor_closed": true,
  "vehicle_connected": true,
  "vehicle_current_a": 16.1,
  "currentA_a": 16.1,
  "currentB_a": 16.1,
  "currentC_a": 16.1,
  "session_energy_wh": 0.0,
  "evse_state": 11
}
```

## Known Wi-Fi Behavior

One test charger temporarily became unreachable on the local LAN.

From the Debian host:

```text
curl: Failed to connect to <wall-connector-ip> port 80
ping: Destination Host Unreachable
ip neigh: INCOMPLETE
```

The web UI therefore keeps known chargers in `known_wall_connectors.json` and
marks them offline rather than deleting them when Wi-Fi is temporarily bad.
