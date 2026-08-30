"""True off-machine backup leg.

A nightly local snapshot (or a copy to a second local disk) still dies with the
machine: fire, theft, or a disk-controller failure takes the backups along with
the live DB. This adds a REMOTE/CLOUD leg: after the nightly local snapshot,
copy the newest-per-family backup to a configured remote target.

SAFETY (zero-outbound-by-default): a network/cloud target (rclone remote)
REFUSES to touch the network unless OPS_ALLOW_LIVE=1 is set in the environment,
so it is inert overnight and during tests. A 'local' target (a directory,
including a scratch "remote" dir or a mounted NAS path) is always allowed.
Config-driven + disabled by default, so it is a no-op until the operator fills
in a real remote and enables it.

Config file: autonomy/backup_offsite_remote.json (not shipped; create it to
enable the leg):
  {"enabled": false, "type": "rclone"|"local", "target": "<remote:path or dir>",
   "families": ["ops.pre-*.db", "ops-*-nightly.db", "vec.pre-*.db"]}

FICTIONAL example (rclone remote; the remote name comes from your own
`rclone config`, and provider credentials live in rclone's config file,
never in this repo):
  {"enabled": true, "type": "rclone", "target": "examplecloud:ops-backups"}
"""
from __future__ import annotations
import glob
import json
import os
import shutil
import subprocess
import sys

import paths

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BACKUP_DIR = str(paths.BACKUPS_DIR)
CONFIG_PATH = os.path.join(_HERE, "backup_offsite_remote.json")
# Newest-per-family glob patterns matching the naming that _db.make_backup and
# the nightly snapshot steps produce. Add your own families (e.g. an external
# mail-mirror DB) via the config file.
DEFAULT_FAMILIES = ["ops.pre-*.db", "ops-*-nightly.db",
                    "vec.pre-*.db", "vec-*-nightly.db"]
NETWORK_TYPES = {"rclone"}  # types that touch the network -> OPS_ALLOW_LIVE gate


def load_config(path: str = CONFIG_PATH) -> dict:
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {"enabled": False, "error": "config unreadable"}
    return {"enabled": False, "reason": "no config file"}


def remote_copy(config: dict, backup_dir: str = DEFAULT_BACKUP_DIR, allow_live: bool | None = None) -> dict:
    """Copy the newest-per-family backup to config['target']. Returns a report."""
    if allow_live is None:
        allow_live = os.environ.get("OPS_ALLOW_LIVE") == "1"
    if not config.get("enabled"):
        return {"skipped": "disabled", "copied": []}
    typ = config.get("type", "local")
    target = config.get("target")
    if not target:
        return {"skipped": "no target configured", "copied": []}
    if typ in NETWORK_TYPES and not allow_live:
        return {"skipped": f"network type '{typ}' refused (OPS_ALLOW_LIVE not set)", "copied": []}
    families = config.get("families", DEFAULT_FAMILIES)
    copied = []
    for pat in families:
        cands = sorted(glob.glob(os.path.join(backup_dir, pat)), key=os.path.getmtime, reverse=True)
        if not cands:
            continue
        newest = cands[0]
        base = os.path.basename(newest)
        if typ == "local":
            os.makedirs(target, exist_ok=True)
            dest = os.path.join(target, base)
            if os.path.exists(dest) and os.path.getsize(dest) == os.path.getsize(newest):
                copied.append({"file": base, "note": "already-present", "size_match": True})
                continue
            shutil.copy2(newest, dest)
            copied.append({"file": base, "size_match": os.path.getsize(dest) == os.path.getsize(newest)})
        elif typ == "rclone":  # only reached when allow_live (checked above)
            r = subprocess.run(["rclone", "copyto", newest, f"{target}/{base}"],
                               capture_output=True, text=True, timeout=3600)
            copied.append({"file": base, "via": "rclone", "rc": r.returncode})
        else:
            copied.append({"file": base, "note": f"unknown type {typ}", "size_match": False})
    return {"target": target, "type": typ, "copied": copied, "skipped": None}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="true off-machine backup leg")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    report = remote_copy(load_config(args.config))
    if args.json:
        print(json.dumps(report, indent=1))
    else:
        if report.get("skipped"):
            print(f"offsite-remote: skipped ({report['skipped']})")
        else:
            ok = sum(1 for c in report["copied"] if c.get("size_match") or c.get("rc") == 0)
            print(f"offsite-remote: {ok}/{len(report['copied'])} copied to {report['target']}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
