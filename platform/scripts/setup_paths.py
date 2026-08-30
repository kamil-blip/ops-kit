"""Write an 'ops-kit.pth' into the ACTIVE environment's site-packages so the
capability directories import flat (import paths, import _db, import brief ...),
matching how the modules reference each other. Run from inside the venv:
    python scripts/setup_paths.py
Re-run any time; it overwrites the .pth. Exit 0 = written + import check passed."""
import os
import site
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRS = ["core", "tools", "memory", "learning", "search", "comms", "brief",
        "tasks", "interfaces", "autonomy", "logging", "hooks", "db"]

sp = site.getsitepackages()
target = None
for cand in sp:
    if os.path.basename(cand) == "site-packages":
        target = cand
        break
if target is None:
    target = sp[0]

pth = os.path.join(target, "ops-kit.pth")
with open(pth, "w", encoding="utf-8") as fh:
    for d in DIRS:
        full = os.path.join(ROOT, d)
        if os.path.isdir(full):
            fh.write(full + "\n")
print(f"wrote {pth}")

sys.path[:0] = [os.path.join(ROOT, d) for d in DIRS]
import paths  # noqa: E402

print(f"import check: paths.ROOT={paths.ROOT}")
print("OK")
