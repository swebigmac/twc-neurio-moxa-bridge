#!/usr/bin/env bash
set -u

echo "===== OS ====="
cat /etc/os-release 2>/dev/null || true
uname -a
echo

echo "===== MOXA MODULES ====="
lsmod | grep -Ei 'mxuport|usbserial|moxa' || true
echo

echo "===== MOXA PORTS ====="
ls -l /dev/ttyMXUSB* /dev/ttyUSB* 2>/dev/null || true
echo

echo "===== SERIAL INTERFACE MODE ====="
for dev in /dev/ttyMXUSB0 /dev/ttyMXUSB1; do
  if [ -e "$dev" ]; then
    setserial -g "$dev" || true
  fi
done
echo

echo "===== RECENT KERNEL LOG ====="
dmesg | grep -Ei 'moxa|mxuport|ttyMXUSB|ttyUSB|decompression|firmware' | tail -n 120 || true
