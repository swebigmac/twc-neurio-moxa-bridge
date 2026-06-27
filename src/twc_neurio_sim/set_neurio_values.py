#!/usr/bin/env python3
"""Small CLI helper for manually changing simulated Neurio values.

The serial simulator reads /etc/twc-neurio-sim/values.json whenever the file's
mtime changes.  This helper is therefore the simplest way to run controlled
experiments:

    sudo ./set_neurio_values.py 10 20 30

The values are interpreted as import/current consumption per phase.  In the
Tesla One app this was verified to appear as L1=10 A, L2=20 A, L3=30 A.
"""

import argparse
import json
from pathlib import Path

CONFIG_PATH = Path("/etc/twc-neurio-sim/values.json")

parser = argparse.ArgumentParser(description="Set test Neurio values for TWC simulator")
parser.add_argument("l1", type=float, help="Phase 1 current in A")
parser.add_argument("l2", type=float, help="Phase 2 current in A")
parser.add_argument("l3", type=float, help="Phase 3 current in A")
parser.add_argument("--voltage", type=float, default=230.0, help="Voltage used to calculate W values")
parser.add_argument("--total-first", action="store_true", help="Put total current first, then L1/L2/L3. Default is L1/L2/L3/total.")
args = parser.parse_args()

total = args.l1 + args.l2 + args.l3

# The confirmed current register ordering for Wall Connector firmware
# 26.18.0 is L1, L2, L3, total.  --total-first is kept as a lab switch because
# early reverse-engineering notes and some Neurio datasets can be ambiguous
# until viewed in Tesla One.
if args.total_first:
    current_a = [total, args.l1, args.l2, args.l3]
else:
    current_a = [args.l1, args.l2, args.l3, total]

# The Wall Connector primarily reacts to current values for load management,
# but it also polls the power register block.  Keep power internally
# consistent with current using a configurable nominal phase voltage.
power_w = [args.l1 * args.voltage, args.l2 * args.voltage, args.l3 * args.voltage, total * args.voltage, 0.0]
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
CONFIG_PATH.write_text(json.dumps({"current_a": current_a, "power_w": power_w}, indent=2) + "\n")
print(f"Wrote {CONFIG_PATH}")
print(f"current_a={current_a}")
print(f"power_w={power_w}")
