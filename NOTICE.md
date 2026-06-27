# Notice and Credits

This project was developed by reverse-engineering and field testing a Tesla Wall
Connector Gen 3 remote-meter setup with a Moxa UPort 1650-16.

## Prior Art

The following open source projects were important references:

- `Klangen82/tesla-wall-connector-control`
  - URL: https://github.com/Klangen82/tesla-wall-connector-control
  - License: MIT
  - Contribution to this project: confirmed the general approach of emulating a
    Neurio/Tesla remote meter over RS485/Modbus for Wall Connector Gen 3 load
    balancing, and documented firmware behavior changes.

- `frankenbubble/twc3-modbus`
  - URL: https://github.com/frankenbubble/twc3-modbus
  - License: GPL-3.0
  - Contribution to this project: supplied known-good Modbus response register
    payloads for identity, handshake, power and current reads.  The identity
    and handshake register constants in this repository are derived from those
    response files.

The GPL-3.0 licensing of `frankenbubble/twc3-modbus` is why this repository is
also licensed under GPL-3.0-or-later.

## Trademarks

Tesla, Wall Connector, Neurio and Generac are trademarks or names belonging to
their respective owners.  This project is unofficial and is not affiliated with,
endorsed by, or supported by Tesla, Neurio, Generac or Moxa.
