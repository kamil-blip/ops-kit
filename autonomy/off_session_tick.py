"""Off-session tick (register as a scheduled task, every ~4 min).

Per tick, while holding the shared single-instance lock:
  1. drain a small batch of generic work_queue handlers (log_interaction,
     link_action_item, extract_action_items, promote_learning) -- only when
     the optional worker/ layer is installed; skipped cleanly otherwise
  2. advance the comms_monitor cursor (mechanics only -- lands pending_gate
     rows for later strong-model judgment; NEVER sends anything)
  3. scan job heartbeats and push an alert for newly-RED critical jobs
     (optional red_alert_tick module; skipped when absent)

Single-instance via the worker layer's shared lock path (or a local fallback):
a second concurrent tick (or a manually-run worker) exits 0 on the lock.
Fail-soft: a drain or comms error is logged and the tick still returns 0 so
the OS scheduler records success.
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import time

import paths

LOG = logging.getLogger("offsession.tick")
_LOGFILE = str(paths.DATA_DIR / "off_session.log")


def _configure_logging() -> None:
    try:
        paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(_LOGFILE, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )


def main() -> int:
    _configure_logging()

    # The work_queue worker layer is optional in this starter kit. When it is
    # installed, share its single-instance lock so a tick never overlaps a
    # manually-run worker; otherwise fall back to a local lock path.
    try:
        from worker import daemon
        lock_path = daemon.SINGLETON_LOCK_PATH
    except ImportError:
        daemon = None
        lock_path = os.path.join(tempfile.gettempdir(), "ops_offsession_worker.lock")
    except Exception as exc:  # noqa: BLE001 -- a broken worker layer must not kill the tick
        LOG.warning("worker layer import failed (%s); drains skipped", exc)
        daemon = None
        lock_path = os.path.join(tempfile.gettempdir(), "ops_offsession_worker.lock")

    release = None
    try:
        from filelock import FileLock, Timeout
        lock = FileLock(lock_path, timeout=0)
        try:
            lock.acquire()
        except Timeout:
            LOG.info("tick skipped: another worker holds %s", lock_path)
            return 0
        release = lock.release
    except ImportError:
        # filelock not installed: atomic-create fallback. A lockfile older
        # than 30 min is treated as stale (crashed tick) and stolen.
        try:
            if time.time() - os.stat(lock_path).st_mtime > 1800:
                os.unlink(lock_path)
        except OSError:
            pass
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            LOG.info("tick skipped: another worker holds %s", lock_path)
            return 0
        release = lambda: os.unlink(lock_path)  # noqa: E731

    # Tick liveness heartbeat (fail-open when the module is absent).
    try:
        from job_heartbeat import heartbeat as _heartbeat
    except Exception:
        import contextlib as _cl

        def _heartbeat(job):
            return _cl.nullcontext(type("_HB", (), {"rows_touched": 0, "exit_note": None})())
    _hb_ctx = _heartbeat("OffSessionTick")
    _hb = _hb_ctx.__enter__()

    t0 = time.monotonic()
    drain = {"claimed": 0, "done": 0, "skipped": 0, "failed": 0, "rejected": 0}
    cm = {"status": "skipped"}
    try:
        # Per-handler filtered drains: hook-time drains alone cannot keep up
        # with inflow, so the tick drains each handler under its own filter --
        # rows for unregistered handlers are never claimed (dispatch would
        # terminally skip them). All are cheap (small-model or deterministic),
        # and input guards pre-reject junk without a model call.
        # Per-handler try split: one handler's import/constructor failure
        # (e.g. a missing API key raising in a handler __init__) must not kill
        # the drains of the others. Each handler imports, constructs, and
        # drains inside its own try block.
        if daemon is not None:
            import importlib
            handler_specs = [
                ("log_interaction", "worker.handlers.log_interaction", "LogInteractionHandler", 40),
                ("link_action_item", "worker.handlers.link_action_item", "LinkActionItemHandler", 20),
                ("extract_action_items", "worker.handlers.extract_action_items", "ExtractActionItemsHandler", 20),
                ("promote_learning", "worker.handlers.promote_learning", "PromoteLearningHandler", 20),
            ]
            for name, mod_path, cls_name, limit in handler_specs:
                try:
                    cls = getattr(importlib.import_module(mod_path), cls_name)
                    part = daemon.run_once({name: cls()}, limit=limit, handler_filter=name)
                    for k in drain:
                        drain[k] += part.get(k, 0)
                except Exception as exc:  # noqa: BLE001 -- never crash the tick on a drain error
                    LOG.warning("drain %s failed: %s", name, exc)
        else:
            LOG.info("worker layer not installed; work_queue drains skipped")

        # comms_monitor.tick(): mechanics only, never sends.
        try:
            import comms_monitor
            cm = comms_monitor.tick()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("comms_monitor tick failed: %s", exc)

        # RED-heartbeat push alert: surface a newly-RED critical job at
        # DETECTION time, not next-session digest. Self-rate-limited to one
        # scan/30min; alerts only newly-RED jobs. Optional module; fail-open.
        red = {"status": "skipped"}
        try:
            import red_alert_tick
            red = red_alert_tick.scan_and_alert()
        except ImportError:
            pass  # optional module not included in this starter kit
        except Exception as exc:  # noqa: BLE001
            LOG.warning("red_alert_tick failed: %s", exc)

        dt = int((time.monotonic() - t0) * 1000)
        LOG.info(
            "tick done in %dms: drain(claimed=%d done=%d rejected=%d failed=%d) "
            "comms(%s landed=%s) red(%s)",
            dt, drain.get("claimed", 0), drain.get("done", 0),
            drain.get("rejected", 0), drain.get("failed", 0),
            cm.get("status"), cm.get("landed"), red.get("status"),
        )
        return 0
    finally:
        try:
            _hb.rows_touched = int(drain.get("done", 0)) + int(drain.get("claimed", 0))
        except Exception:
            pass
        try:
            _hb_ctx.__exit__(None, None, None)
        except Exception:
            pass
        try:
            if release is not None:
                release()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
