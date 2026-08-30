"""Off-session nightly maintenance (schedule it daily, e.g. 03:00 local).

Recurring retention + hygiene for the ops database:
  1. archive terminal work_queue rows (done/skipped/failed/rejected) finished
     > RETENTION_DAYS ago into work_queue_archive (+ record dedup_keys in
     work_queue_dedup_index)
  2. compact the WAL via wal_checkpoint(TRUNCATE), then FTS5 optimize +
     freelist reclaim
  3. episodes materializer catch-up (backfill_episodes otherwise only runs
     inside a full daily sync; running it here keeps episodes from going
     stale when every sync takes the fast path)
  4. nightly DB snapshots (VACUUM INTO), local rotation, optional offsite +
     remote legs
  5. embedding catch-up (FAQs, reference-doc chunks, action items;
     approved-only for FAQs so the semantic index mirrors status='approved'
     exactly)
  6. drift reconcile, FAQ expiry flagging, identity merge-candidate sieve,
     retention pack, growth snapshot, learning promotion drain, test suite

Single-instance via the shared worker lock (or a local fallback when the
optional worker layer is absent; its queue steps then skip cleanly).
Fail-soft + idempotent: every step swallows its own failure and records a
nightly_step_runs row. NEVER sends anything.
"""
from __future__ import annotations

import paths

import logging
import os
import sys
from pathlib import Path

_MODULE_DIR = Path(__file__).resolve().parent

LOG = logging.getLogger("offsession.nightly")
_LOGFILE = str(paths.DATA_DIR / "off_session.log")
RETENTION_DAYS = 7
KEEP_PER_FAMILY = 7   # backup rotation: newest N snapshots kept per family

# vec-repair guard: while this sentinel file exists, skip every step that
# re-embeds into vec_* tables. A vec-store repair moves/rebuilds the vec0
# tables; an embed run mid-repair would re-create empty shadows and split
# writes between the old and new store.
_VEC_REPAIR_GUARD = Path(str(paths.PLAN_STATE_DIR / "VEC_REPAIR_IN_PROGRESS"))


class _SkipStep(Exception):
    """Sentinel: a nightly step was deliberately skipped (not a failure)."""


_RUN_STAMP = None  # per-run stamp for nightly_step_runs (set in _run_nightly)


def _table_exists(conn, name: str) -> bool:
    """Feature-detect an optional table so steps degrade gracefully."""
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


class _step:
    '''Per-step telemetry: records one nightly_step_runs row per executed
    step (ok/failed/skipped) and SWALLOWS exceptions exactly like the
    try/except blocks it replaces (fail-soft chain semantics unchanged).
    _SkipStep -> status 'skipped'. Opens its own short-lived connection so it
    never holds locks across a step.'''
    def __init__(self, name, rows=None):
        self.name = name
        self.rows = rows          # optional callable or int set by the body via .rows
    def __enter__(self):
        import time as _t
        self._t0 = _t.monotonic()
        import datetime as _dt
        # UTC like the rest of the DB -- a local-time offset inside one run
        # breaks cross-table correlation.
        self._started = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        return self
    def __exit__(self, exc_type, exc, tb):
        import time as _t
        ms = int((_t.monotonic() - self._t0) * 1000)
        if exc_type is None:
            status, err = "ok", None
        elif exc_type is _SkipStep:
            status, err = "skipped", None
        else:
            status, err = "failed", str(exc)[:800]
            LOG.warning("step %s failed: %s", self.name, exc)
        try:
            import sqlite3 as _sq
            c = _sq.connect(str(paths.DB_PATH), timeout=30)
            c.execute("PRAGMA busy_timeout=30000")
            rows = self.rows() if callable(self.rows) else self.rows
            c.execute("INSERT INTO nightly_step_runs (run_stamp, step_name, started_at, duration_ms, status, error, rows) VALUES (?,?,?,?,?,?,?)",
                      (_RUN_STAMP, self.name, self._started, ms, status, err, rows))
            c.commit(); c.close()
        except Exception as tex:
            LOG.warning("step telemetry write failed for %s: %s", self.name, tex)
        return True   # swallow -- fail-soft chain semantics preserved


def _configure_logging() -> None:
    os.makedirs(os.path.dirname(_LOGFILE), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(_LOGFILE, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )


def _heartbeat_ctx(job):
    # liveness ledger (fail-open -- observability never breaks the job)
    try:
        from job_heartbeat import heartbeat
        return heartbeat(job)
    except Exception:
        import contextlib
        return contextlib.nullcontext(type("_HB", (), {"rows_touched": 0, "exit_note": None})())


def _oneshot_heartbeat(job: str, note: str) -> None:
    """Best-effort standalone heartbeat row for a sub-job (own dead-man
    tracking, independent of the nightly wrapper). No-op when the
    job_heartbeat helper does not ship next to this module."""
    try:
        import subprocess as _sp
        _jh = _MODULE_DIR / "job_heartbeat.py"
        if _jh.is_file():
            _sp.run([sys.executable, str(_jh), job, "--oneshot", "--note", note[:200]],
                    capture_output=True, timeout=30)
    except Exception:
        pass


def _prune_backup_family(dirpath: str, keep: int = KEEP_PER_FAMILY):
    """Keep the newest `keep` .db files per backup family in `dirpath`.

    Family = filename with the timestamp token stripped, so e.g.
    ops-20300101-030000-nightly.db and ops-20300102-030000-nightly.db are
    siblings. Returns the list of pruned {file, size} dicts (already
    deleted). Without rotation, nightly VACUUM INTO snapshots accumulate
    unboundedly.
    """
    import re as _re
    _ts_re = _re.compile(r"\d{8}[-_]?\d{0,6}")
    fams = {}
    for _n in os.listdir(dirpath):
        _p = os.path.join(dirpath, _n)
        if os.path.isfile(_p) and _n.endswith(".db"):
            fams.setdefault(_ts_re.sub("<TS>", _n), []).append(
                (_n, os.path.getmtime(_p), os.path.getsize(_p)))
    pruned = []
    for _members in fams.values():
        _members.sort(key=lambda x: -x[1])
        for _n, _mt, _sz in _members[keep:]:
            try:
                os.remove(os.path.join(dirpath, _n))
                pruned.append({"file": _n, "size": _sz})
            except OSError as prune_exc:
                LOG.warning("backup prune failed for %s: %s", _n, prune_exc)
    return pruned


def main() -> int:
    _configure_logging()
    if "--smoke" in sys.argv:
        # smoke = heartbeat row from the wrapped entrypoint, zero side effects
        with _heartbeat_ctx("OffSessionNightly") as hb:
            hb.exit_note = "smoke"
            hb.rows_touched = 0
        LOG.info("smoke run: heartbeat row written, no work performed")
        return 0
    with _heartbeat_ctx("OffSessionNightly") as _hb:
        rc = _run_nightly(_hb)
    return rc


def fts_maintenance(conn) -> tuple[int, int]:
    """Compact the FTS5 shadow tables ('optimize' merges the b-tree segments)
    and reclaim freelist pages (incremental_vacuum; the DB is
    auto_vacuum=INCREMENTAL). Both idempotent -- a second run is a fast no-op.
    Returns (optimized_count, total_fts). Fail-soft per index."""
    import sqlite3 as _sq
    ftss = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%\\_fts' ESCAPE '\\' "
        "AND sql LIKE '%fts5%' COLLATE NOCASE")]
    optimized = 0
    for f in ftss:
        try:
            conn.execute(f'INSERT INTO "{f}"("{f}") VALUES(\'optimize\')')
            optimized += 1
        except _sq.OperationalError:
            pass  # external-content / contentless fts without an optimize path
    conn.execute("PRAGMA incremental_vacuum")
    conn.commit()
    return optimized, len(ftss)


def _run_nightly(_hb) -> int:
    global _RUN_STAMP
    import time
    import datetime as _dtrs
    # UTC run_stamp so stamps correlate with the rest of the DB.
    _RUN_STAMP = _dtrs.datetime.now(_dtrs.timezone.utc).strftime("%Y%m%d-%H%M%S")
    from filelock import FileLock, Timeout
    # The work_queue worker layer is OPTIONAL in this starter kit (same
    # invariant as off_session_tick.py). When installed, share its
    # single-instance lock and run its queue steps; otherwise fall back to a
    # local lock path and skip the worker-only steps cleanly.
    try:
        from worker import daemon, work_queue as qmod
        _lock_path = daemon.SINGLETON_LOCK_PATH
    except ImportError:
        import tempfile
        daemon = qmod = None
        _lock_path = os.path.join(tempfile.gettempdir(), "ops_offsession_worker.lock")

    lock = FileLock(_lock_path, timeout=0)
    try:
        lock.acquire()
    except Timeout:
        LOG.info("nightly skipped: another worker holds %s", _lock_path)
        return 0

    t0 = time.monotonic()
    _vec_repair = _VEC_REPAIR_GUARD.exists()
    if _vec_repair:
        LOG.warning(
            "VEC_REPAIR_IN_PROGRESS guard present (%s) -> skipping embed + "
            "episodes-catch-up steps this run", _VEC_REPAIR_GUARD)
    try:
        # 1. work_queue retention: archive terminal rows (worker layer only).
        archived = {"archived": 0}
        with _step("work_queue_archive"):
            if qmod is None:
                LOG.info("worker layer not installed; work_queue archive skipped")
                raise _SkipStep()
            archived = qmod.archive_terminal_rows(older_than_days=RETENTION_DAYS)

        # 2. WAL compaction.
        ckpt = None
        with _step("wal_checkpoint"):
            if qmod is not None:
                ckpt = qmod.wal_checkpoint_truncate()
            else:
                # inline fallback: plain SQLite WAL compaction via the
                # shipped connector; same PRAGMA, no worker telemetry
                import _db as _dbw
                _wc = _dbw.connect()
                try:
                    ckpt = tuple(_wc.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
                finally:
                    _wc.close()

        # 3. FTS5 optimize + freelist reclaim. Runs after the work_queue
        # archive delete (freelist to reclaim) and compacts the FTS shadow
        # tables. Idempotent + fail-soft.
        fts_maint = "skipped"
        with _step("fts_maintenance"):
            import _db as _dbm
            _fc = _dbm.connect()
            try:
                _opt, _tot = fts_maintenance(_fc)
                fts_maint = "optimized %d/%d + incremental_vacuum" % (_opt, _tot)
                LOG.info("fts_maintenance: %s", fts_maint)
            finally:
                _fc.close()

        # 4. episodes materializer catch-up.
        episodes_added = 0
        if _vec_repair:
            LOG.info("skip episodes catch-up: VEC_REPAIR_IN_PROGRESS")
        with _step("episodes_catchup"):
            if _vec_repair:
                raise _SkipStep()
            import backfill_episodes as bfe
            conn = bfe._connect()
            try:
                # Incremental: resume from the newest episode minus 1 day of
                # overlap (content_hash dedup makes the overlap a no-op).
                row = conn.execute(
                    "SELECT COALESCE(MAX(ts), '1970-01-01') AS ts FROM episodes"
                ).fetchone()
                since = (row["ts"] if row else "1970-01-01")[:10]
                episodes_added += bfe.backfill_emails(conn, since)
                episodes_added += bfe.backfill_discord(conn, since)
                episodes_added += bfe.backfill_beeper(conn, since)
                conn.commit()
            finally:
                conn.close()

        # 5. backups. VACUUM INTO gives a consistent point-in-time copy
        # without locking writers; the wal_checkpoint above already compacted
        # the main DB.
        _BK = str(paths.BACKUPS_DIR)
        os.makedirs(_BK, exist_ok=True)
        _VEC_LIVE = str(getattr(paths, "VEC_DB_PATH", paths.DATA_DIR / "vec.db"))

        # 5a. Fresh consistent vec.db snapshot so rotation + offsite legs
        # include it. vec.db holds every embedding (2-file layout) -- without
        # its own snapshot it is a single point of loss.
        vec_backup = "skipped"
        with _step("backup_vec"):
            import datetime as _dtv
            import sqlite3 as _sq
            if os.path.exists(_VEC_LIVE):
                _vdst = os.path.join(
                    _BK, "vec-%s.db" % _dtv.datetime.now().strftime("%Y%m%d-%H%M%S"))
                _vc = _sq.connect(_VEC_LIVE, timeout=60)
                _vc.execute("PRAGMA busy_timeout=60000")
                _vc.execute("VACUUM INTO ?", (_vdst,))
                _vc.close()
                vec_backup = os.path.basename(_vdst)
        LOG.info("vec.db nightly backup: %s", vec_backup)

        # 5b. Nightly snapshots for the other single-point-of-loss DBs: the
        # main ops.db (source of truth) and, when configured, the external
        # gmail-to-sqlite mirror.
        db_backups = {}
        with _step("backup_nightly_dbs"):
            _targets = [("ops", str(paths.DB_PATH))]
            _gmail_db = getattr(paths, "GMAIL_DB_PATH", None)
            if _gmail_db:
                _targets.append(("messages", str(_gmail_db)))
            for _label, _live in _targets:
                db_backups[_label] = "skipped"
                try:
                    import datetime as _dtn
                    import sqlite3 as _sqn
                    if os.path.exists(_live):
                        _ndst = os.path.join(
                            _BK, "%s-%s-nightly.db" % (
                                _label, _dtn.datetime.now().strftime("%Y%m%d-%H%M%S")))
                        _nc = _sqn.connect(_live, timeout=90)
                        _nc.execute("PRAGMA busy_timeout=90000")
                        _nc.execute("VACUUM INTO ?", (_ndst,))
                        _nc.close()
                        db_backups[_label] = os.path.basename(_ndst)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("%s nightly backup failed: %s", _label, exc)
        LOG.info("nightly db snapshots: %s", db_backups)

        # 5c. local rotation: keep the newest N snapshots per backup family
        # so the backups dir never grows unboundedly.
        rotated = "skipped"
        with _step("backup_rotation"):
            _pruned_local = _prune_backup_family(_BK, keep=KEEP_PER_FAMILY)
            rotated = "pruned=%d" % len(_pruned_local)
            if _pruned_local:
                LOG.info("local backup rotation: removed %d file(s), %.2f GB",
                         len(_pruned_local),
                         sum(p["size"] for p in _pruned_local) / 1e9)

        # 5d. offsite leg: copy every backup file missing from a second disk
        # (set OPS_OFFSITE_DIR to e.g. another drive or a mounted share;
        # unset = skip). Integrity-check each copy, then apply the same
        # keep-N-per-family retention with a purge log.
        with _step("offsite_leg"):
            import glob
            import json as _json
            import shutil as _sh
            import sqlite3 as _sq2
            from datetime import datetime as _pdt, timezone as _ptz
            dst_dir = os.environ.get("OPS_OFFSITE_DIR", "").strip()
            if not dst_dir:
                LOG.info("offsite leg: OPS_OFFSITE_DIR not configured, skipping")
                raise _SkipStep()
            os.makedirs(dst_dir, exist_ok=True)
            for newest in sorted(glob.glob(os.path.join(_BK, "*.db")),
                                 key=os.path.getmtime, reverse=True):
                dest = os.path.join(dst_dir, os.path.basename(newest))
                if os.path.exists(dest):
                    continue
                _sh.copy2(newest, dest)
                try:
                    # Immutable read-only open so the integrity check never
                    # mints -wal/-shm sidecars next to the backup files.
                    _ic = _sq2.connect(f"file:{dest}?immutable=1", uri=True, timeout=60)
                    res = _ic.execute("PRAGMA integrity_check").fetchone()
                    _ic.close()
                    LOG.info("offsite leg: copied %s (integrity=%s)",
                             os.path.basename(newest), res[0] if res else "?")
                except Exception as ic_exc:  # noqa: BLE001
                    LOG.warning("offsite integrity_check %s failed: %s",
                                os.path.basename(dest), ic_exc)
            # Keep-N-per-family retention (operator-approved policy): the
            # offsite dir would otherwise be copy-only accumulation with
            # nothing ever removed. Deletions are appended to a purge log.
            pruned = _prune_backup_family(dst_dir, keep=KEEP_PER_FAMILY)
            if pruned:
                _plog = os.path.join(
                    dst_dir, "purge-log-%s.json" % _pdt.now(_ptz.utc).strftime("%Y%m%d"))
                _entries = []
                if os.path.exists(_plog):
                    try:
                        _entries = _json.load(open(_plog, encoding="utf-8")).get("deleted", [])
                    except Exception:  # noqa: BLE001
                        _entries = []
                _json.dump({"policy": "keep newest %d per family" % KEEP_PER_FAMILY,
                            "purged_at": _pdt.now(_ptz.utc).isoformat(timespec="seconds"),
                            "deleted": _entries + pruned},
                           open(_plog, "w", encoding="utf-8"), indent=1)
                LOG.info("offsite prune: removed %d file(s), %.2f GB",
                         len(pruned), sum(p["size"] for p in pruned) / 1e9)

        # 5e. TRUE off-machine (remote/cloud) leg, after the local legs.
        # Config-driven + disabled by default -> inert no-op until the
        # operator configures a remote. The network transport never fires
        # without OPS_ALLOW_LIVE, which the overnight run never sets.
        offsite_remote = "skipped"
        with _step("offsite_remote_leg"):
            import backup_offsite_remote as _bor
            _rr = _bor.remote_copy(_bor.load_config())
            if _rr.get("skipped"):
                offsite_remote = _rr["skipped"]
                LOG.info("offsite_remote_leg: %s", offsite_remote)
                raise _SkipStep()
            offsite_remote = "%d copied to %s" % (len(_rr["copied"]), _rr.get("target"))
            LOG.info("offsite_remote_leg: %s", offsite_remote)

        # 6. FAQ embedding catch-up: keep vec_faqs current without a manual
        # run. APPROVED-ONLY on purpose -- the semantic FAQ index must mirror
        # status='approved' exactly (retrieval must never surface an
        # unreviewed proposed answer). Local embedding model, idempotent
        # (no-ops when vec_faqs is current), fail-soft.
        faqs_embedded = "skipped"
        with _step("embed_faqs"):
            if _vec_repair:
                faqs_embedded = "skipped:vec-repair"
                LOG.info("skip embed_faqs: VEC_REPAIR_IN_PROGRESS")
                raise _SkipStep()
            import subprocess as _sp
            r = _sp.run(
                [sys.executable, str(paths.ROOT / "search" / "embed_faqs.py")],
                capture_output=True, text=True, timeout=900)
            faqs_embedded = "rc=%d" % r.returncode
            if r.returncode != 0:
                LOG.warning("embed_faqs stderr: %s", (r.stderr or "")[-500:])

        # 7. doc-chunk + action-item embedding: (i) chunk any reference_docs
        # added since the last run, when a chunker script is present at
        # search/chunk_reference_docs.py (--new-only never re-chunks an
        # existing doc, so chunk ids stay stable and their
        # vec_reference_doc_chunks embeddings never orphan; a full re-chunk
        # DELETEs+reinserts every row, minting new ids). Chunking writes only
        # the main DB, so it runs even during a vec repair. (ii) embed the
        # new doc chunks + action items -- vec-guarded like faqs. Idempotent
        # (no-op when current), fail-soft.
        doc_chunk = "skipped"
        with _step("doc_chunk_embed"):
            import subprocess as _sp6
            _chunker = paths.ROOT / "search" / "chunk_reference_docs.py"
            if _chunker.is_file():
                rc = _sp6.run(
                    [sys.executable, str(_chunker), "--new-only"],
                    capture_output=True, text=True, timeout=600)
                doc_chunk = "chunk_rc=%d" % rc.returncode
                if rc.returncode != 0:
                    LOG.warning("chunk_reference_docs stderr: %s", (rc.stderr or "")[-500:])
            else:
                doc_chunk = "chunk:absent"
                LOG.info("doc chunker not included in this starter kit "
                         "(%s missing); embedding existing chunks only", _chunker)
            if _vec_repair:
                doc_chunk += " embed:skipped-vec-repair"
                LOG.info("skip doc/action embed: VEC_REPAIR_IN_PROGRESS")
                raise _SkipStep()
            for _emb in ("embed_reference_doc_chunks.py", "embed_action_items.py"):
                er = _sp6.run(
                    [sys.executable, str(paths.ROOT / "search" / _emb)],
                    capture_output=True, text=True, timeout=1800)
                if er.returncode != 0:
                    LOG.warning("%s stderr: %s", _emb, (er.stderr or "")[-500:])
            doc_chunk += " embed:done"
        LOG.info("doc-chunk + action-item embed: %s", doc_chunk)

        # 8. drift reconcile: run all drift checks, then auto-open one
        # action_items_inbox remediation proposal per open unsuppressed
        # alert. Idempotent (signature in source_ref), capped at
        # drift_check.RECONCILE_DAILY_CAP per UTC day. Fail-soft.
        drift_reconciled = "skipped"
        with _step("drift_reconcile"):
            import drift_check
            r = drift_check.run_reconcile()
            rec = r.get("reconcile", {})
            drift_reconciled = "opened=%d skipped=%d" % (
                len(rec.get("opened", [])), len(rec.get("skipped", [])))
            if rec.get("opened"):
                LOG.info("drift reconcile opened: %s",
                         [o["signature"] for o in rec["opened"]])

        # 9. FAQ expiry: flag approved FAQs whose answers carry time-anchored
        # prose with no placeholders (event-scoped claims that rot) into
        # review_queue. Flag-only; NEVER auto-edits an answer.
        faq_expired = 0
        with _step("faq_expiry"):
            faq_expired = _faq_expiry_sweep(dry_run="--faq-expiry-dry" in sys.argv)

        # 10. identity merge-candidate sieve: shared-identifier clusters ->
        # merge_candidates pending. Self-stops after 2 consecutive empty
        # nights (n4_kv sieve state).
        sieve = "skipped"
        with _step("merge_candidate_sieve"):
            sieve = _merge_candidate_sieve()

        # 11. FAQ occurrence detector + linkage: keep the FAQ demand-evidence
        # stream (faq_occurrences) growing and linked to canonical FAQs.
        # Bounded recent-window capture (discord + beeper) when a detector
        # script is present, then a $0 fuzzy-match backfill that (re)links
        # occurrences; an attach trigger reconciles faqs.ask_count. File kill
        # switch <plan-state>/FAQ_DETECTOR_DISABLED; the capture leg needs an
        # LLM API key (skipped cleanly if absent), the backfill leg is
        # key-free. Idempotent (UNIQUE source+source_row_id; backfill only
        # touches faq_id IS NULL). Fail-soft.
        faq_detect = "skipped"
        with _step("faq_detector"):
            faq_detect = _faq_detector_step()

        # 12. retention pack: staged archives + caps + expiry pruning for the
        # high-churn operational tables (operator-approved delete class; see
        # retention_pack.py for the policy list). Own heartbeat row so the
        # dead-man checker tracks it independently of the nightly wrapper.
        retention = "skipped"
        with _step("retention_pack"):
            import retention_pack
            _rp = retention_pack.run(actor="retention_pack:nightly", verbose=False)
            retention = "; ".join(
                "%s=%s" % (k, (v.get("moved") if isinstance(v, dict) and "moved" in v else v))
                for k, v in sorted(_rp.items()))[:200] or "no-op"
            _oneshot_heartbeat("RetentionPack", "ok: " + retention)

        # 13. growth snapshot: one table_rowcounts row per main-schema table
        # (COUNT(*)) so table growth is trackable over time. Main schema only
        # -- vec/attached tables live in their own DB files and are not
        # visible to a plain main-DB connection. Skips fts shadows.
        # st.rows = #tables.
        growth = "skipped"
        with _step("growth_snapshot") as st:
            import sqlite3 as _sqg
            _gc = _sqg.connect(str(paths.DB_PATH), timeout=90)
            _gc.execute("PRAGMA busy_timeout=90000")
            try:
                _tables = [r[0] for r in _gc.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%'").fetchall()]
                _n = 0
                for _tname in _tables:
                    try:
                        _cnt = _gc.execute('SELECT COUNT(*) FROM "%s"' % _tname).fetchone()[0]
                    except Exception:  # noqa: BLE001 -- skip a table that won't COUNT
                        continue
                    _gc.execute(
                        "INSERT INTO table_rowcounts (captured_at, table_name, rows) "
                        "VALUES (datetime('now'), ?, ?)", (_tname, _cnt))
                    _n += 1
                _gc.commit()
                st.rows = _n
                growth = "%d tables" % _n
            finally:
                _gc.close()
        LOG.info("growth snapshot: %s", growth)

        # 14. promote_learnings: the promote_learning handler runs one
        # LLM-CLI batch call per queue item -- far too slow for hook-time
        # drains, so the nightly owns it with a real budget. Fail-soft: a
        # dead CLI fails the step, not the chain.
        promote_res = "skipped"
        with _step("promote_learnings") as st:
            import subprocess as _spl
            import sqlite3 as _sqpl
            pending_pl = 0
            try:
                _plc = _sqpl.connect(str(paths.DB_PATH), timeout=30)
                _plc.execute("PRAGMA busy_timeout=30000")
                pending_pl = _plc.execute(
                    "SELECT COUNT(*) FROM work_queue WHERE status='pending' AND handler='promote_learning'"
                ).fetchone()[0]
                _plc.close()
            except Exception:  # noqa: BLE001
                pass
            if pending_pl == 0:
                promote_res = "skipped:queue-empty"
                raise _SkipStep()
            if qmod is None:
                promote_res = "skipped:no-worker"
                LOG.info("promote_learnings: worker layer not installed, skipping")
                raise _SkipStep()
            # Locate the queue drain runner inside the worker package.
            import worker as _workerpkg
            _wfile = getattr(_workerpkg, "__file__", None)
            _wdir = (Path(_wfile).resolve().parent if _wfile
                     else Path(list(_workerpkg.__path__)[0]))
            _drain = _wdir / "drain_inline.py"
            if not _drain.is_file():
                promote_res = "skipped:no-drain-runner"
                LOG.info("promote_learnings: worker drain runner not present "
                         "(%s), skipping", _drain)
                raise _SkipStep()
            r = _spl.run([sys.executable, str(_drain),
                          "--handler", "promote_learning", "--limit", "3", "--timeout", "660"],
                         capture_output=True, text=True, timeout=2100)
            last = (r.stdout or "").strip().splitlines()[-1:] or ["?"]
            promote_res = "rc=%d %s" % (r.returncode, last[0][:160])
            st.rows = pending_pl
            if r.returncode != 0:
                raise RuntimeError("promote_learning drain failed: %s" % promote_res)
        LOG.info("promote_learnings: %s", promote_res)

        # 15. nightly tests: run the repo test suite as the last nightly step
        # so a green heartbeat means the suite passed. Test dir is
        # config-driven (OPS_TESTS_DIR env var, default <root>/tests); the
        # step skips cleanly when no test dir exists. returncode != 0 ->
        # RuntimeError so _step records status='failed'. Own heartbeat
        # oneshot row so the dead-man checker tracks it independently.
        # st.rows = parsed passed-count.
        nightly_tests = "skipped"
        with _step("nightly_tests") as st:
            import subprocess as _spt
            _tests_dir = os.environ.get("OPS_TESTS_DIR", "").strip() or str(paths.ROOT / "tests")
            if not os.path.isdir(_tests_dir):
                nightly_tests = "skipped:no-tests-dir"
                LOG.info("nightly tests: no test dir at %s (set OPS_TESTS_DIR), skipping",
                         _tests_dir)
                raise _SkipStep()
            r = _spt.run(
                [sys.executable, "-m", "pytest", _tests_dir, "-q"],
                capture_output=True, text=True, timeout=1800)
            _tlines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
            last_line = _tlines[-1] if _tlines else ""
            import re as _re
            _pm = _re.search(r"(\d+) passed", last_line)
            if _pm:
                st.rows = int(_pm.group(1))
            nightly_tests = "rc=%d %s" % (r.returncode, last_line[:160])
            _oneshot_heartbeat("NightlyTests", nightly_tests)
            if r.returncode != 0:
                raise RuntimeError("pytest failed: " + last_line)
        LOG.info("nightly tests: %s", nightly_tests)

        dt = int((time.monotonic() - t0) * 1000)
        LOG.info(
            "nightly done in %dms: archived=%d wal_checkpoint=%s episodes=+%d rotate=%s remote=%s faqs=%s doc_chunk=%s drift=%s faq_expiry=%d sieve=%s faq_detect=%s retention=%s growth=%s promote=%s tests=%s",
            dt, archived.get("archived", 0), ckpt, episodes_added, rotated, offsite_remote, faqs_embedded, doc_chunk, drift_reconciled, faq_expired, sieve, faq_detect, retention, growth, promote_res, nightly_tests,
        )
        try:
            _hb.rows_touched = int(archived.get("archived", 0)) + int(episodes_added)
        except Exception:
            pass
        return 0
    finally:
        try:
            lock.release()
        except Exception:
            pass


def _faq_expiry_sweep(dry_run: bool = False) -> int:
    """Flag stale approved FAQs into review_queue (flag-only).

    Stale = answer has zero {{placeholders}} AND carries time-anchored prose
    ('right now', 'this round', 'currently', "aren't available", 'at the
    moment'), i.e. an event-scoped claim that will silently rot. Dedupe on an
    open review_queue row per FAQ. Returns rows flagged this run.
    """
    import json as _json
    import sqlite3 as _sq
    conn = _sq.connect(str(paths.DB_PATH), timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    markers = ("right now", "this round", "currently", "aren''t available",
               "aren't available", "at the moment")
    flagged = 0
    try:
        rows = conn.execute(
            "SELECT id, faq_id, topic, answer_canonical FROM faqs "
            "WHERE status='approved' AND answer_canonical NOT LIKE '%{{%'").fetchall()
        for fid, faq_id, topic, ans in rows:
            low = (ans or "").lower()
            hits = [m for m in markers if m in low]
            if not hits:
                continue
            dup = conn.execute(
                "SELECT 1 FROM review_queue WHERE queue_type='faq_expiry' AND status='pending' "
                "AND payload LIKE ?", ('%"faq_row_id": ' + str(fid) + ',%',)).fetchone()
            if dup:
                continue
            if dry_run:
                LOG.info("faq expiry DRY: would flag faq id=%d (%s) markers=%s", fid, topic, hits)
                flagged += 1
                continue
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO review_queue (queue_type, payload, priority, status, queued_by, queued_at, trace_json) "
                "VALUES ('faq_expiry', ?, 3, 'pending', 'off_session_nightly', datetime('now'), ?)",
                (_json.dumps({"faq_row_id": fid, "faq_id": faq_id, "topic": topic,
                              "markers": hits, "why": "time-anchored prose, zero placeholders"}),
                 _json.dumps({"source": "off_session_nightly faq-expiry sweep",
                              "source_table": "faqs", "source_id": fid})))
            conn.commit()
            flagged += 1
            LOG.info("faq expiry: flagged faq id=%d (%s) markers=%s", fid, topic, hits)
    finally:
        conn.close()
    return flagged


def _keyring_service() -> str:
    """OS keyring service name for API keys (config.toml [keys], default
    'ops-kit'). Fail-open: config problems never break a nightly step."""
    try:
        import config as _cfgmod
        return _cfgmod.get("keyring_service", "ops-kit") or "ops-kit"
    except Exception:
        return "ops-kit"


def _faq_detector_step() -> str:
    """FAQ occurrence detector + linkage.

    Two legs: (1) capture -- run comms/faq_detector.py (an LLM classifier)
    over a bounded recent window on discord + beeper, writing new
    faq_occurrences rows. The detector script is OPTIONAL and not part of
    this starter kit; the leg feature-detects it and skips cleanly when
    absent. (2) backfill -- run comms/faq_occurrence_backfill.py (fuzzy
    match, $0) to (re)link NULL-faq_id occurrences to canonical FAQs, which
    fires the ask_count trigger; also feature-detected.

    Guards: file kill switch <plan-state>/FAQ_DETECTOR_DISABLED skips both
    legs; the capture leg is skipped cleanly when no Gemini key is resolvable
    (the key-free backfill leg still runs). Fail-soft per leg.
    """
    import datetime as _dt
    import os
    import subprocess
    from pathlib import Path as _P

    disabled = _P(str(paths.PLAN_STATE_DIR / "FAQ_DETECTOR_DISABLED"))
    if disabled.exists():
        LOG.info("faq detector: kill switch present (%s), skipping", disabled)
        return "disabled"

    comms_dir = paths.ROOT / "comms"
    detector = str(comms_dir / "faq_detector.py")
    since = (_dt.date.today() - _dt.timedelta(days=3)).isoformat()

    def _have_gemini_key():
        if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            return True
        try:
            import keyring
            return bool(keyring.get_password(_keyring_service(), "GEMINI_API_KEY"))
        except Exception:
            return False

    captured = "skipped:no-key"
    if os.path.exists(detector) and _have_gemini_key():
        import re as _re
        parts = []
        auth_failures = []
        _auth_pat = _re.compile(
            r"(?i)403|PERMISSION_DENIED|denied access|unauthorized|401|invalid api key|api key")
        for src in ("discord", "beeper"):
            try:
                r = subprocess.run(
                    [sys.executable, detector, "--source", src,
                     "--since", since, "--limit", "400"],
                    capture_output=True, text=True, timeout=1800,
                    cwd=str(comms_dir))
                parts.append("%s:rc=%d" % (src, r.returncode))
                if r.returncode != 0:
                    LOG.warning("faq detector %s stderr: %s", src, (r.stderr or "")[-400:])
                    if _auth_pat.search((r.stderr or "") + (r.stdout or "")):
                        auth_failures.append(src)
            except Exception as exc:  # noqa: BLE001
                parts.append("%s:err" % src)
                LOG.warning("faq detector %s failed: %s", src, exc)
        captured = ",".join(parts)
        # An auth/403-dead capture leg is DETECTOR-DOWN, not a silent green.
        # Write the kill-file (nightlies then skip cleanly with 'disabled')
        # and fire ONE drift alert so the outage is visible.
        if auth_failures:
            captured += ",DETECTOR-DOWN:auth"
            try:
                disabled.write_text(
                    "auto: capture leg auth/403-dead on %s at %s; "
                    "delete this file after the API key/project is restored.\n"
                    % (",".join(auth_failures), _dt.date.today().isoformat()),
                    encoding="utf-8")
            except OSError as exc:
                LOG.warning("faq detector kill-file write failed: %s", exc)
            try:
                import sqlite3 as _sq
                import drift_check as _dc
                _c = _sq.connect(str(paths.DB_PATH), timeout=30)
                _dc._upsert_alert(_c, {
                    "alert_type": "faq_detector_down", "severity": "warn",
                    "summary": "FAQ occurrence detector capture leg is auth-dead "
                               "(%s); kill-file written, capture disabled until "
                               "the key/project is restored" % ",".join(auth_failures),
                    "detail": {"legs": auth_failures, "since": since}})
                _c.commit()
                _c.close()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("faq detector-down alert write failed: %s", exc)
    elif not os.path.exists(detector):
        captured = "skipped:no-detector"
        LOG.info("faq detector: capture script not included in this starter "
                 "kit (%s missing); backfill leg still runs", detector)

    backfilled = "skipped"
    _backfill = comms_dir / "faq_occurrence_backfill.py"
    if _backfill.is_file():
        try:
            r = subprocess.run(
                [sys.executable, str(_backfill),
                 "--commit", "--run-id", _dt.datetime.now().strftime("nightly-%Y%m%d"),
                 "--phase", "nightly"],
                capture_output=True, text=True, timeout=600)
            backfilled = "rc=%d" % r.returncode
            if r.returncode != 0:
                LOG.warning("faq backfill stderr: %s", (r.stderr or "")[-400:])
        except Exception as exc:  # noqa: BLE001
            LOG.warning("faq backfill failed: %s", exc)
    else:
        backfilled = "skipped:not-included"
        LOG.info("faq backfill: script not included in this starter kit (%s missing)",
                 _backfill)

    LOG.info("faq detector nightly: capture=%s backfill=%s (since=%s)",
             captured, backfilled, since)
    return "capture=%s backfill=%s" % (captured, backfilled)


def _merge_candidate_sieve() -> str:
    """Identity-floor merge-candidate sieve (generic people dedup).

    Shared-identifier clusters (2-3 live people on one email/discord/other
    identifier, denylist-filtered) -> merge_candidates status='pending',
    signal='identifier_shared', source tag 'identity-sieve' in evidence.
    Never proposes a pair that already has ANY merge_candidates row (incl.
    rejected -- no re-litigating). Self-stops after 2 consecutive empty
    nights (n4_kv sieve_empty_nights/sieve_stopped).
    """
    import itertools
    import json as _json
    import sqlite3 as _sq

    conn = _sq.connect(str(paths.DB_PATH), timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        if conn.execute("SELECT 1 FROM n4_kv WHERE k='sieve_stopped' AND v='1'").fetchone():
            return "self-stopped"
        clusters = conn.execute("""
            SELECT pi.id_type, pi.id_value_norm, GROUP_CONCAT(DISTINCT pi.person_id)
            FROM person_identifiers pi
            JOIN people p ON p.id = pi.person_id AND p.merged_into IS NULL
              AND (p.is_real_person IS NULL OR p.is_real_person = 1)
            WHERE pi.id_value_norm NOT IN (SELECT pattern FROM identifier_denylist WHERE kind='exact')
              AND NOT EXISTS (SELECT 1 FROM identifier_denylist dl WHERE dl.kind='like'
                              AND pi.id_value_norm LIKE dl.pattern)
            GROUP BY 1, 2 HAVING COUNT(DISTINCT pi.person_id) BETWEEN 2 AND 3""").fetchall()
        inserted = 0
        # Attribute the writes when the audit-context table is present
        # (feature-detected: the audit triggers read actor from it).
        _has_ctx = _table_exists(conn, "_audit_context")
        conn.execute("BEGIN IMMEDIATE")
        if _has_ctx:
            conn.execute("UPDATE _audit_context SET actor='writer:off_session_nightly/sieve', "
                         "source_ref='identity-sieve', set_at=CURRENT_TIMESTAMP WHERE id=1")
        try:
            for id_type, value, pids in clusters:
                ids = sorted({int(x) for x in pids.split(",")})
                for a, b in itertools.combinations(ids, 2):
                    dup = conn.execute(
                        "SELECT 1 FROM merge_candidates WHERE (canonical_id=? AND duplicate_id=?) "
                        "OR (canonical_id=? AND duplicate_id=?)", (a, b, b, a)).fetchone()
                    if dup:
                        continue
                    conn.execute(
                        "INSERT INTO merge_candidates (canonical_id, duplicate_id, signal, confidence, "
                        "evidence, status, created_at) VALUES (?,?,?,?,?,'pending',datetime('now'))",
                        (a, b, "identifier_shared", 0.6,
                         _json.dumps({"source": "identity-sieve", "id_type": id_type, "identifier": value})))
                    inserted += 1
        finally:
            if _has_ctx:
                conn.execute("UPDATE _audit_context SET actor=NULL, source_ref=NULL, set_at=NULL WHERE id=1")
        if inserted == 0:
            row = conn.execute("SELECT v FROM n4_kv WHERE k='sieve_empty_nights'").fetchone()
            empty = (int(row[0]) if row and row[0] else 0) + 1
            conn.execute("INSERT OR REPLACE INTO n4_kv (k, v, updated_at) VALUES "
                         "('sieve_empty_nights', ?, datetime('now'))", (str(empty),))
            if empty >= 2:
                conn.execute("INSERT OR REPLACE INTO n4_kv (k, v, updated_at) VALUES "
                             "('sieve_stopped', '1', datetime('now'))")
        else:
            conn.execute("INSERT OR REPLACE INTO n4_kv (k, v, updated_at) VALUES "
                         "('sieve_empty_nights', '0', datetime('now'))")
        conn.commit()
        LOG.info("merge-candidate sieve: %d clusters scanned, %d new candidates", len(clusters), inserted)
        return "+%d" % inserted
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
