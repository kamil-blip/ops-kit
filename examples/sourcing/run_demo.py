"""Run the sourcing demo end to end: seed -> search -> feedback.

    python examples/sourcing/run_demo.py
    python examples/sourcing/run_demo.py --reset   # remove everything the demo wrote
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable


def run(script: str, *args: str) -> int:
    print(f"\n=== {script} {' '.join(args)} ===")
    sys.stdout.flush()
    return subprocess.call([PY, str(HERE / script), *args])


def main(argv) -> int:
    if "--reset" in argv:
        return run("seed_candidates.py", "--reset")
    for step in (("seed_candidates.py",), ("search.py",), ("feedback.py",)):
        rc = run(*step)
        if rc != 0:
            print(f"step failed: {step[0]} (exit {rc})")
            return rc
    print("\n=== summary ===")
    print("brief -> rubric (rubric-skill/rubric.json) -> 20 fictional candidates seeded through the steward bus")
    print("-> gates then points with an evidence quote per criterion -> two sent, one flagged as the bet")
    print("-> hiring-manager feedback stored as a learning that surfaces on the next similar brief.")
    print("Everything above ran offline against data/ops.db. Re-run is idempotent; --reset removes it all.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
