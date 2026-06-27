# Moxa UPort 1650-16 Notes

This project was developed with a Moxa UPort 1650-16 USB serial adapter on a
small Debian host.

The important design point is that the Moxa gives every Wall Connector its own
RS485 port.  The Wall Connectors do not share one multidrop bus with each other;
instead the Linux process mirrors the same Neurio meter behavior on many
independent serial ports.

## Logical Port Mapping

For the UPort 1650-16 with the Moxa Linux driver:

| Physical Moxa port | Linux symlink | Backing tty |
| ---: | --- | --- |
| 1 | `/dev/ttyMXUSB0` | `/dev/ttyUSB0` |
| 2 | `/dev/ttyMXUSB1` | `/dev/ttyUSB1` |
| 3 | `/dev/ttyMXUSB2` | `/dev/ttyUSB2` |
| ... | ... | ... |
| 16 | `/dev/ttyMXUSB15` | `/dev/ttyUSB15` |

The first two chargers in the lab used:

```text
Physical port 1 -> /dev/ttyMXUSB0 -> WC1
Physical port 2 -> /dev/ttyMXUSB1 -> WC2
```

## Required Interface Mode

The ports must be configured as RS485 2-wire.  On the Moxa driver this is done
with `setserial`:

```bash
for i in $(seq 0 15); do
  setserial /dev/ttyMXUSB$i port 0x1
done
```

Moxa's documented values:

| `setserial port` value | Interface |
| --- | --- |
| `0x0` | RS232 |
| `0x1` | RS485 2-wire |
| `0x2` | RS422 |
| `0x3` | RS485 4-wire |

Check current mode:

```bash
setserial -g /dev/ttyMXUSB0 /dev/ttyMXUSB1 /dev/ttyMXUSB15
```

Known-good output looked like:

```text
/dev/ttyMXUSB0, UART: 16550A, Port: 0x0001, IRQ: 0, Flags: low_latency
/dev/ttyMXUSB1, UART: 16550A, Port: 0x0001, IRQ: 0, Flags: low_latency
/dev/ttyMXUSB15, UART: 16550A, Port: 0x0001, IRQ: 0, Flags: low_latency
```

If the port still reports `Port: 0x0000`, it is still RS232 mode and the Wall
Connector traffic will appear dead.

## Driver Used

The tested system used:

- Debian 13
- Kernel `6.12.94+deb13-amd64`
- Moxa UPort Linux Kernel 6.x driver v6.2
- Kernel module: `mxuport`

After loading the driver, the kernel log showed:

```text
MOXA UPort 1650-16
MOXA UPort converter now attached to ttyUSB0
...
MOXA UPort converter now attached to ttyUSB15
```

## Kernel 6.12.x Compression Issue

On the tested Debian host, Moxa's install produced:

```text
/lib/modules/6.12.94+deb13-amd64/misc/mxuport.ko.xz
```

`modprobe mxuport` failed after reboot with:

```text
modprobe: ERROR: could not insert 'mxuport': Invalid argument
decompression failed with status 6
```

Workaround used in the lab:

```bash
cd /lib/modules/$(uname -r)/misc
xz -dk mxuport.ko.xz
mv mxuport.ko.xz mxuport.ko.xz.disabled
depmod -a
modprobe mxuport
```

After that, `modprobe -D mxuport` should resolve to the uncompressed `.ko`.

## systemd Startup Ordering

The simulator service does three important things before opening serial ports:

```ini
ExecStartPre=/sbin/modprobe mxuport
ExecStartPre=/bin/sh -c 'for n in $(seq 1 30); do ok=1; for i in $(seq 0 15); do [ -e /dev/ttyMXUSB$i ] || ok=0; done; [ "$ok" = "1" ] && exit 0; sleep 1; done; exit 1'
ExecStartPre=/bin/sh -c 'for i in $(seq 0 15); do /bin/setserial /dev/ttyMXUSB$i port 0x1; done'
```

The default service and simulator expect all 16 ports of a UPort 1650-16.  Use
manual `--port` arguments only for custom device names or a smaller adapter.

## Troubleshooting

Check devices:

```bash
ls -l /dev/ttyMXUSB* /dev/ttyUSB*
```

Check driver:

```bash
lsmod | grep -Ei 'mxuport|usbserial|moxa'
dmesg | grep -Ei 'moxa|mxuport|ttyUSB|ttyMXUSB|decompression|firmware' | tail -n 120
```

Check serial mode:

```bash
setserial -g /dev/ttyMXUSB0 /dev/ttyMXUSB1 /dev/ttyMXUSB15
```

Watch simulator logs:

```bash
journalctl -u twc-neurio-sim.service -f
```

Watch web logs:

```bash
journalctl -u twc-neurio-web.service -f
```

Run the environment helper:

```bash
sudo ./tools/check_environment.sh
```
