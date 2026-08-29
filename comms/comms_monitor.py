"""Comms Monitor -- DRAFTS ONLY. It never sends. Ever.

A sibling to the Context Steward. Each tick polls new inbound (Gmail/Discord/Beeper)
since a per-channel cursor, applies a cheap DETERMINISTIC pre-filter (obvious skips
only: outgoing, closed thread, automated sender), lands surviving candidates in
comms_drafts as status='pending_gate', and notifies the operator. That is all the
headless tick does -- no LLM, no send.

The JUDGMENT (is a reply warranted? + write the reply) is strong-model-only. It runs
when an agentic session drains the queue (gate_and_draft / apply_gate+apply_draft),
or autonomously via the Anthropic API IF one is configured (gate_and_draft_via_api,
fail-closed when the brain is unreachable -- it NEVER guesses with a cheap model and
NEVER sends). There is deliberately no send code path in this module.
"""
from __future__ import annotations
import paths
import hashlib, json, os, re, sys, time, sqlite3

from _db import connect
import steward_brain as BR

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK = os.path.join(HERE, "comms_monitor.lock")
STEWARD_LOCK = os.path.join(HERE, "steward.lock")

# email_threads.status values that mean the thread is closed -> no draft.
CLOSED_THREAD = {"resolved", "resolved_elsewhere", "no_action_needed", "info_captured",
                 "no_inbound", "ignore", "stale", "replied", "expired"}
# senders we never draft a reply to (automated / notification addresses).
AUTOMATED_SENDERS = ("noreply", "no-reply", "donotreply", "do-not-reply", "notifications@",
                     "mailer-daemon", "postmaster@", "calendar-notification", "luma-mail.com",
                     "atlassian.net", "jira@", "@sentry", "notify@", "bounce", "mailer@",
                     "calendar.luma-mail", "user.luma-mail", "@github.com")
# notification-template content -> never a reply (event/registration blasts).
# Matched ONLY against the sender's own unquoted text (see _unquoted_body): quoted
# originals and mailing-list footers used to trigger these on genuine human replies.
# Bare "unsubscribe" was dropped from this list: it lives in every Google Groups
# footer. Replaced by the footer-anchored UNSUBSCRIBE_FOOTER_PATTERNS check below.
# Bare "registered for" also narrowed to the second-person template phrasings: human
# prose like "people who registered for the event" was falsely skipped, while the
# welcome-aboard blasts always say "you're registered for".
NOTIFICATION_PATTERNS = ("registered for your event", "is starting tomorrow", "new registration",
                         "joined your event", "you're registered for", "you are registered for",
                         "purchased a ticket", "rsvp'd",
                         "left your event", "your event is live", "event reminder",
                         "cancelled their registration", "canceled their registration",
                         "view this post on the web")
# Footer-anchored unsubscribe phrasings (newsletter/blast boilerplate). Only checked in
# the TAIL of the unquoted body, so a human sentence like "please unsubscribe me" or a
# quoted footer never triggers a skip.
UNSUBSCRIBE_FOOTER_PATTERNS = ("to unsubscribe", "unsubscribe from", "unsubscribe here",
                               "unsubscribe |", "unsubscribe at any time",
                               "click here to unsubscribe", "manage your subscription",
                               "update your email preferences")
UNSUBSCRIBE_FOOTER_TAIL = 600  # chars of unquoted-body tail scanned for the above
POLL_LIMIT = 200


# ───────────────────────── lock + heartbeat ─────────────────────────
def _acquire_lock(max_age_s: int = 600) -> bool:
    if os.path.exists(LOCK):
        try:
            if time.time() - os.path.getmtime(LOCK) < max_age_s:
                return False
        except OSError:
            pass
    try:
        with open(LOCK, "w") as f:
            f.write(str(os.getpid()))
        return True
    except OSError:
        return False


def _release_lock():
    try:
        os.remove(LOCK)
    except OSError:
        pass


def _heartbeat(conn, status, drafts_made=0, error=None):
    conn.execute(
        "UPDATE comms_monitor_health SET last_tick_at=datetime('now'), status=?, "
        "ticks_total=COALESCE(ticks_total,0)+1, drafts_made=COALESCE(drafts_made,0)+?, "
        "last_error=?, pid=?, updated_at=datetime('now') WHERE id=1",
        (status, drafts_made, error, os.getpid()))
    conn.commit()


def _idem(channel, thread_id, msg_id):
    return hashlib.sha256(f"{channel}:{thread_id}:{msg_id}".encode()).hexdigest()


def _load_operator_discord_id() -> str:
    """The operator's own Discord user id, used by the freshness/handled re-check
    below to recognize the operator's replies. Config-driven, empty by default
    (the Discord handled-check degrades gracefully until configured).

    Sources, in order:
      1. OPERATOR_DISCORD_ID env var
      2. <repo-root>/config.toml: [operator] discord_user_id
      3. "" (Discord reply detection disabled)

    Example config.toml entry (FICTIONAL id, replace with your own):
      # [operator]
      # discord_user_id = "123456789012345678"
    """
    env = os.environ.get("OPERATOR_DISCORD_ID", "").strip()
    if env:
        return env
    cfg = paths.ROOT / "config.toml"
    if cfg.is_file():
        try:
            import tomllib
            with open(cfg, "rb") as f:
                data = tomllib.load(f)
            return str((data.get("operator", {}) or {}).get("discord_user_id", "") or "").strip()
        except Exception:
            pass
    return ""


OPERATOR_DISCORD_ID = _load_operator_discord_id()


def _already_handled(conn, cand) -> tuple[bool, str]:
    """Freshness + handled re-check (drafts-only safety; the root-cause fix for
    stale drafts). A candidate is 'handled' if, AFTER the inbound message, the
    operator already replied OR reacted in the same thread/chat/channel:
      gmail   -> a later is_outgoing email in the thread. Covers BOTH text replies
                 and Gmail emoji reactions (those land as is_outgoing email rows,
                 body like '<name> reacted via Gmail').
      beeper  -> a later is_outgoing row in the chat. Covers text replies AND
                 reactions (reactions are stored as is_outgoing message_type='reaction').
      discord -> a later message from the operator's author_id in the channel
                 (requires OPERATOR_DISCORD_ID). Text replies only -- Discord
                 reactions are not synced, so an emoji-only reaction with no text
                 on a Discord DM still slips through.
    Fail-OPEN: any error returns (False, ...) so the re-check never strands a tick.
    """
    ch = cand.get("channel"); mid = str(cand.get("msg_id") or "")
    if not mid:
        return (False, "")
    try:
        if ch == "gmail":
            r = conn.execute("SELECT thread_id, timestamp FROM emails WHERE id=?", (mid,)).fetchone()
            if not r:
                return (False, "")
            tid, ts = r[0], r[1]
            n = conn.execute("SELECT COUNT(*) FROM emails WHERE thread_id=? AND is_outgoing=1 AND timestamp>?",
                             (tid, ts)).fetchone()[0]
            return (n > 0, "operator_replied_or_reacted_after" if n else "")
        if ch == "beeper":
            r = conn.execute("SELECT chat_id, timestamp FROM beeper_messages WHERE id=?", (mid,)).fetchone()
            if not r:
                return (False, "")
            chat, ts = r[0], r[1]
            n = conn.execute("SELECT COUNT(*) FROM beeper_messages WHERE chat_id=? AND COALESCE(is_outgoing,0)=1 AND timestamp>?",
                             (chat, ts)).fetchone()[0]
            return (n > 0, "operator_replied_or_reacted_after" if n else "")
        if ch == "discord":
            if not OPERATOR_DISCORD_ID:
                return (False, "")  # can't recognize the operator's replies until configured
            r = conn.execute("SELECT channel_id, timestamp FROM discord_messages WHERE id=?", (mid,)).fetchone()
            if not r:
                return (False, "")
            chan, ts = r[0], r[1]
            n = conn.execute("SELECT COUNT(*) FROM discord_messages WHERE channel_id=? AND author_id=? AND timestamp>?",
                             (chan, OPERATOR_DISCORD_ID, ts)).fetchone()[0]
            return (n > 0, "operator_replied_after" if n else "")
    except Exception as exc:  # noqa: BLE001 — fail-open: never block a tick on the re-check
        return (False, f"handled_check_error:{str(exc)[:80]}")
    return (False, "")


def _sweep_superseded(conn) -> int:
    """Re-check every open draft (pending_gate / pending_draft / draft) and flip any
    that the operator has since handled (replied or reacted) to status='superseded_handled'.
    Catches drafts that were fresh when landed but went stale while they sat in the
    queue -- the exact failure mode where a morning draft rots by evening. Returns
    the number superseded."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, channel, thread_id, inbound_msg_id FROM comms_drafts "
                        "WHERE status IN ('pending_gate','pending_draft','draft')").fetchall()
    flipped = 0
    for r in rows:
        handled, reason = _already_handled(conn, {"channel": r["channel"], "thread_id": r["thread_id"],
                                                  "msg_id": r["inbound_msg_id"]})
        if handled:
            conn.execute("UPDATE comms_drafts SET status='superseded_handled', "
                         "gate_reason=COALESCE(NULLIF(?,''),gate_reason), updated_at=datetime('now') WHERE id=?",
                         (reason, r["id"]))
            flipped += 1
    if flipped:
        conn.commit()
    return flipped


def _cursor(conn, source):
    r = conn.execute("SELECT last_id FROM sync_state WHERE source=?", (source,)).fetchone()
    return r[0] if r else None


def _set_cursor(conn, source, last_id):
    conn.execute("INSERT INTO sync_state(source,last_id,last_sync) VALUES(?,?,datetime('now')) "
                 "ON CONFLICT(source) DO UPDATE SET last_id=excluded.last_id, last_sync=excluded.last_sync",
                 (source, str(last_id)))
    conn.commit()


# ───────────────────────── pollers (one per channel) ─────────────────────────
def poll_gmail(conn, limit=POLL_LIMIT):
    cur = _cursor(conn, "comms_monitor_gmail")
    cur = int(cur) if cur is not None else 0
    rows = conn.execute(
        "SELECT e.id, e.thread_id, e.sender_email, e.person_id, e.subject, e.body, e.gmail_message_id, "
        "       COALESCE(t.status,'pending') AS tstatus "
        "FROM emails e LEFT JOIN email_threads t ON t.thread_id=e.thread_id "
        "WHERE e.is_outgoing=0 AND e.is_deleted=0 AND e.id>? ORDER BY e.id LIMIT ?",
        (cur, limit)).fetchall()
    cands = []
    maxid = cur
    for eid, tid, se, pid, subj, body, gmid, tstatus in rows:
        maxid = max(maxid, eid)
        cands.append({"channel": "gmail", "thread_id": tid, "msg_id": str(eid), "ext_id": gmid,
                      "sender_email": se, "sender_person_id": pid, "subject": subj,
                      "text": (body or "")[:4000], "thread_status": tstatus})
    return cands, ("comms_monitor_gmail", maxid)


def poll_discord(conn, limit=POLL_LIMIT):
    cur = _cursor(conn, "comms_monitor_discord")
    cur = int(cur) if cur is not None else 0
    rows = conn.execute(
        "SELECT id, thread_id, channel_id, author_id, person_id, content FROM discord_messages "
        "WHERE id>? AND content IS NOT NULL AND content!='' ORDER BY id LIMIT ?", (cur, limit)).fetchall()
    cands, maxid = [], cur
    for mid, tid, chan, author, pid, content in rows:
        maxid = max(maxid, int(mid))  # id is a snowflake stored as TEXT; cast before max()
        cands.append({"channel": "discord", "thread_id": tid or chan, "msg_id": str(mid), "ext_id": str(mid),
                      "sender_email": None, "sender_person_id": pid, "subject": None,
                      "text": (content or "")[:4000], "thread_status": "pending"})
    return cands, ("comms_monitor_discord", maxid)


def poll_beeper(conn, limit=POLL_LIMIT):
    # beeper_messages.id is a Matrix '$...:server' event string, NOT a snowflake, so it
    # can't be int()/max()'d or ordered numerically. The cursor is keyed to the INTEGER
    # rowid instead: the cursor in sync_state is the last-seen rowid.
    cur = _cursor(conn, "comms_monitor_beeper")
    try:
        cur = int(cur) if cur not in (None, "") else 0
    except (TypeError, ValueError):
        cur = 0
    rows = conn.execute(
        "SELECT rowid, id, chat_id, sender_id, person_id, text FROM beeper_messages "
        "WHERE rowid>? AND COALESCE(is_outgoing,0)=0 AND text IS NOT NULL AND text!='' ORDER BY rowid LIMIT ?",
        (cur, limit)).fetchall()
    cands, maxid = [], cur
    for rid, mid, chat, sender, pid, text in rows:
        maxid = max(maxid, int(rid))
        cands.append({"channel": "beeper", "thread_id": chat, "msg_id": str(mid), "ext_id": str(mid),
                      "sender_email": None, "sender_person_id": pid, "subject": None,
                      "text": (text or "")[:4000], "thread_status": "pending"})
    return cands, ("comms_monitor_beeper", maxid)


POLLERS = {"gmail": poll_gmail, "discord": poll_discord, "beeper": poll_beeper}


# ───────────────────────── deterministic pre-filter (obvious skips only) ─────────────────────────
# Reply-quote marker: "On Mon, Jun 29, 2026 at 3:14 PM Name <a@b.c> wrote:" (may wrap
# across two lines, so re.S with a bounded width). Everything from the first match down
# is quoted original text, not the sender's own words.
_REPLY_QUOTE_RE = re.compile(r"(?m)^\s*On .{0,400}?wrote:\s*$", re.S)
# Trailing mailing-list / Google Groups footer blocks, plus quote/forward markers: from
# a line starting with one of these, everything below is not the sender's own text.
_FOOTER_MARKERS = ("you received this message because",
                   "to unsubscribe from this group",
                   "-----original message-----",
                   "---------- forwarded message")


def _unquoted_body(body: str) -> str:
    """Return only the sender's OWN text: cut everything from the reply-quote marker
    ('On ... wrote:'), drop '>'-quoted lines, and cut trailing mailing-list/Google
    Groups footer blocks. NOTIFICATION_PATTERNS must only match what THIS sender wrote:
    before this fix, genuine reply-needed inbound on actionable threads were skipped
    because the quoted original or the Groups footer contained a notification phrase
    (e.g. 'unsubscribe')."""
    m = _REPLY_QUOTE_RE.search(body)
    if m:
        body = body[:m.start()]
    lines = []
    for ln in body.splitlines():
        low = ln.strip().lower()
        if low.startswith(">"):
            continue
        if any(low.startswith(f) for f in _FOOTER_MARKERS):
            break  # trailing footer block: everything from here down is boilerplate
        lines.append(ln)
    return "\n".join(lines)


# ── DB-persisted prefilter rules, unioned with the hardcoded seed sets ──
_DB_RULES_CACHE = None


def _load_db_prefilter_rules(force: bool = False):
    """Active comms_prefilter_rules grouped by rule_type as (pattern, channel) tuples.
    Cached: the monitor is a long-running tick loop, so a newly-activated rule takes
    effect on the next process start or force=True. Read-only, never raises -- a load
    failure falls back to the hardcoded seed sets so the tick can't break on it."""
    global _DB_RULES_CACHE
    if _DB_RULES_CACHE is not None and not force:
        return _DB_RULES_CACHE
    rules = {"automated_sender": [], "notification_pattern": [], "unsubscribe_footer": []}
    try:
        c = connect(readonly=True)
        for rt, pat, ch in c.execute(
                "SELECT rule_type, lower(pattern), lower(COALESCE(channel,'all')) "
                "FROM comms_prefilter_rules WHERE active=1"):
            rules.setdefault(rt, []).append((pat, ch))
        c.close()
    except Exception:
        pass
    _DB_RULES_CACHE = rules
    return rules


def prefilter(cand, db_rules=None) -> tuple[bool, str]:
    """Return (keep, reason). CHEAP + deterministic -- only obvious skips. The real
    reply-warranted JUDGMENT is the strong-model gate, never this. Active
    comms_prefilter_rules rows are unioned with the hardcoded seed sets; with no
    active rows this is byte-for-byte the seed behavior."""
    se = (cand.get("sender_email") or "").lower()
    ch = (cand.get("channel") or "").lower()
    if db_rules is None:
        db_rules = _load_db_prefilter_rules()
    def _pats(rtype):
        return [p for (p, rch) in db_rules.get(rtype, []) if rch in ("all", ch)]
    if any(a in se for a in AUTOMATED_SENDERS) or any(a in se for a in _pats("automated_sender")):
        return False, "automated_sender"
    if (cand.get("thread_status") or "pending") in CLOSED_THREAD:
        return False, f"thread_closed:{cand['thread_status']}"
    body = (cand.get("text") or "")
    if not body.strip():
        return False, "empty_body"
    # Match notification templates against the sender's own unquoted text only. If the
    # message is ALL quote/footer (forward with no comment), fall back to the full body
    # so pure notification blasts are still caught (same behavior as before the fix).
    own = _unquoted_body(body)
    low = (own if own.strip() else body).lower()
    if any(p in low for p in NOTIFICATION_PATTERNS) or any(p in low for p in _pats("notification_pattern")):
        return False, "notification_template"
    tail = low[-UNSUBSCRIBE_FOOTER_TAIL:]
    if any(p in tail for p in UNSUBSCRIBE_FOOTER_PATTERNS) or any(p in tail for p in _pats("unsubscribe_footer")):
        return False, "notification_template"
    return True, "candidate"


# Freemail / shared-mailbox domains: never proposable as an automated_sender rule
# (they carry real people, and rules are substring-matched against sender_email).
_FREEMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "yahoo.com", "ymail.com", "icloud.com", "me.com", "mac.com", "proton.me",
    "protonmail.com", "aol.com", "gmx.com", "gmx.de", "mail.com", "zoho.com",
    "fastmail.com", "hey.com", "pm.me", "qq.com", "163.com", "126.com",
})


def aggregate_prefilter_candidates(conn, min_count: int = 10):
    """Cluster repeated gate_reason on skipped_no_reply drafts and propose candidate
    prefilter rules for HUMAN REVIEW (nothing auto-activates; a person activates chosen
    rows into comms_prefilter_rules). Proposals are constrained to a specific non-freemail
    sender domain OR a >=10-char quoted phrase from the reason, so a proposal cannot be a
    freemail domain or a tiny substring that would catch a real person. READ-ONLY: returns
    candidates, never writes a rule."""
    from collections import Counter
    rows = conn.execute(
        "SELECT gate_reason, COUNT(*) n FROM comms_drafts WHERE status='skipped_no_reply' "
        "AND gate_reason IS NOT NULL AND TRIM(gate_reason)!='' "
        "GROUP BY gate_reason HAVING n>=? ORDER BY n DESC", (min_count,)).fetchall()
    out = []
    for gate_reason, n in rows:
        senders = conn.execute(
            "SELECT sender_email, channel, COUNT(*) c FROM comms_drafts WHERE status='skipped_no_reply' "
            "AND gate_reason=? GROUP BY 1,2", (gate_reason,)).fetchall()
        domains, chans = Counter(), Counter()
        for se, ch, c in senders:
            if se and "@" in se:
                domains[se.split("@")[-1].lower()] += c
            if ch:
                chans[ch] += c
        proposal = None
        if domains:
            top_dom, top_c = domains.most_common(1)[0]
            # NEVER propose a freemail/shared domain as an automated_sender rule: it is
            # applied as a substring test in prefilter(), so 'gmail.com' would silently
            # skip every real person on gmail. A cluster on a freemail domain is a
            # content/semantic pattern, not a sender-domain one.
            if top_c >= max(min_count, 0.8 * n) and top_dom not in _FREEMAIL_DOMAINS:
                proposal = {"rule_type": "automated_sender", "pattern": top_dom,
                            "channel": chans.most_common(1)[0][0] if len(chans) == 1 else "all"}
        if proposal is None:
            # >=10 chars so a proposed notification_pattern is specific enough not to
            # match ordinary human prose (it is substring-tested against body text).
            mq = re.search(r"'([^']{10,80})'|\"([^\"]{10,80})\"", gate_reason)
            if mq:
                proposal = {"rule_type": "notification_pattern",
                            "pattern": (mq.group(1) or mq.group(2)).lower(), "channel": "all"}
        out.append({"gate_reason": gate_reason, "count": n, "proposal": proposal,
                    "top_domains": dict(domains.most_common(3)), "channels": dict(chans)})
    return out


# ───────────────────────── land (the ONLY write; comms_drafts is operational) ─────────────────────────
def land(conn, cand, status, *, reply_needed=None, gate_reason=None, draft_text=None, context_json=None):
    idem = _idem(cand["channel"], cand["thread_id"], cand["msg_id"])
    existing = conn.execute("SELECT id,status FROM comms_drafts WHERE idempotency_key=?", (idem,)).fetchone()
    if existing:
        conn.execute("UPDATE comms_drafts SET status=COALESCE(?,status), reply_needed=COALESCE(?,reply_needed), "
                     "gate_reason=COALESCE(?,gate_reason), draft_text=COALESCE(?,draft_text), "
                     "context_snapshot_json=COALESCE(?,context_snapshot_json), updated_at=datetime('now') WHERE id=?",
                     (status, reply_needed, gate_reason, draft_text, context_json, existing[0]))
        conn.commit()
        return existing[0], False
    cur = conn.execute(
        "INSERT INTO comms_drafts(idempotency_key,channel,thread_id,sender_person_id,sender_email,"
        "inbound_msg_id,inbound_snippet,reply_needed,gate_reason,draft_text,context_snapshot_json,status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (idem, cand["channel"], cand["thread_id"], cand.get("sender_person_id"), cand.get("sender_email"),
         cand["msg_id"], (cand.get("text") or "")[:500], reply_needed, gate_reason, draft_text, context_json, status))
    conn.commit()
    return cur.lastrowid, True


def notify(conn, draft_id, summary):
    """Record a notification (bus_event + flag). The agentic layer sends the actual
    PushNotification when it surfaces the queue; the monitor itself does not push."""
    conn.execute("UPDATE comms_drafts SET notified=1 WHERE id=?", (draft_id,))
    conn.execute("INSERT INTO bus_events(session_id,event_type,summary,details_json,ts) "
                 "VALUES('comms-monitor','comms_draft', ?, ?, datetime('now'))",
                 (summary[:300], json.dumps({"draft_id": draft_id})))
    conn.commit()


def _os_notify(title: str, body: str) -> None:
    """Best-effort real OS toast (Windows). Drafts-only: it tells the operator a draft
    is waiting, it does NOT send anything. Never raises -- a notification failure must
    not fail the tick. One consolidated toast per tick, not one per draft."""
    try:
        import subprocess
        ps = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
            "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            "$x=$t.GetElementsByTagName('text');"
            f"$x.Item(0).AppendChild($t.CreateTextNode('{title}')) | Out-Null;"
            f"$x.Item(1).AppendChild($t.CreateTextNode('{body}')) | Out-Null;"
            "$n=[Windows.UI.Notifications.ToastNotification]::new($t);"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('CommsMonitor').Show($n);"
        )
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       timeout=12, capture_output=True)
    except Exception:  # noqa: BLE001 — notifications are best-effort only
        pass


# ───────────────────────── the tick (mechanics only -- headless-safe) ─────────────────────────
def tick(channels=("gmail", "discord", "beeper")) -> dict:
    if not _acquire_lock():
        return {"status": "locked"}
    t0 = time.monotonic()
    conn = connect()
    # PAUSE SWITCH: the operator can pause draft generation without killing the
    # tick loop. Pause: UPDATE steward_config SET value='paused' WHERE key='comms_draft_generation'
    # Resume: UPDATE steward_config SET value='active' WHERE key='comms_draft_generation'
    try:
        _row = conn.execute(
            "SELECT value FROM steward_config WHERE key='comms_draft_generation'"
        ).fetchone()
        if _row and _row[0] == "paused":
            # Stamp the health row BEFORE returning, so a paused monitor reads
            # 'paused' with a fresh tick instead of being indistinguishable from
            # a dead one (a frozen last_tick_at makes every health surface
            # scream STALE).
            try:
                _heartbeat(conn, "paused")
            except Exception:  # noqa: BLE001 — health stamp must not break the pause
                pass
            conn.close()
            _release_lock()
            return {"status": "paused_by_config"}
    except Exception:
        pass  # missing table/row = active (fail-open keeps the monitor alive)
    out = {"status": "ok", "polled": 0, "landed": 0, "skipped": 0, "superseded": 0, "lane_errors": {}}
    # Per-lane isolation: one channel's crash must NOT strand the others.
    for ch in channels:
        try:
            cands, (src, maxid) = POLLERS[ch](conn)
            out["polled"] += len(cands)
            for cand in cands:
                keep, reason = prefilter(cand)
                if not keep:
                    land(conn, cand, "skipped_prefilter", gate_reason=reason)
                    out["skipped"] += 1
                    continue
                # Freshness/handled re-check: if the operator already replied or reacted
                # after this inbound, do NOT queue a draft (root-cause fix for stale drafts).
                handled, hreason = _already_handled(conn, cand)
                if handled:
                    land(conn, cand, "superseded_handled", gate_reason=hreason)
                    out["superseded"] += 1
                    continue
                did, created = land(conn, cand, "pending_gate", gate_reason="awaiting gate")
                if created:
                    notify(conn, did, f"[{ch}] inbound from {cand.get('sender_email') or cand.get('sender_person_id')} awaiting draft")
                    out["landed"] += 1
            _set_cursor(conn, src, maxid)
        except Exception as exc:  # noqa: BLE001 — isolate this lane, keep the others alive
            out["lane_errors"][ch] = str(exc)[:300]
    try:
        # Sweep open drafts that the operator has handled since they landed (freshness fix).
        out["superseded"] += _sweep_superseded(conn)
        if out["landed"]:
            _os_notify("Comms drafts waiting", f"{out['landed']} new inbound need a reply (drafts only).")
        status_s = "error" if out["lane_errors"] else "ok"
        err = ("; ".join(f"{k}:{v}" for k, v in out["lane_errors"].items()) or None)
        _heartbeat(conn, status_s, drafts_made=out["landed"], error=err)
        out["status"] = status_s
        out["ms"] = int((time.monotonic() - t0) * 1000)
        return out
    finally:
        conn.close()
        _release_lock()


# ───────────────────────── judgment path (agentic or API; fail-closed) ─────────────────────────
def pending_for_judgment(conn, limit=50):
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM comms_drafts WHERE status='pending_gate' ORDER BY id LIMIT ?", (limit,)).fetchall()
    # Surface-time freshness re-check: drop (and supersede) anything the operator handled since it landed.
    fresh = []
    for r in rows:
        handled, reason = _already_handled(conn, {"channel": r["channel"], "thread_id": r["thread_id"],
                                                  "msg_id": r["inbound_msg_id"]})
        if handled:
            conn.execute("UPDATE comms_drafts SET status='superseded_handled', "
                         "gate_reason=COALESCE(NULLIF(?,''),gate_reason), updated_at=datetime('now') WHERE id=?",
                         (reason, r["id"]))
            continue
        fresh.append(r)
    conn.commit()
    return fresh


def apply_gate(conn, draft_id, reply_needed: bool, gate_reason: str):
    """Land a gate verdict (called by an agentic strong-model session, the brain)."""
    status = "pending_draft" if reply_needed else "skipped_no_reply"
    conn.execute("UPDATE comms_drafts SET reply_needed=?, gate_reason=?, status=?, updated_at=datetime('now') WHERE id=?",
                 (1 if reply_needed else 0, gate_reason[:500], status, draft_id))
    conn.commit()
    return status


def apply_draft(conn, draft_id, draft_text: str, context_json: str | None = None):
    """Land an agentically-written draft. status -> 'draft'. NEVER sends."""
    conn.execute("UPDATE comms_drafts SET draft_text=?, context_snapshot_json=COALESCE(?,context_snapshot_json), "
                 "status='draft', updated_at=datetime('now') WHERE id=?", (draft_text, context_json, draft_id))
    conn.commit()


def gate_and_draft_via_api(conn, draft_id) -> dict:
    """Autonomous judgment path -- only fires if an Anthropic brain is configured.
    Fail-closed: on BrainUnavailable it leaves the row pending (NEVER a cheap-model
    guess, NEVER a send)."""
    r = conn.execute("SELECT channel,thread_id,sender_email,inbound_snippet FROM comms_drafts WHERE id=?", (draft_id,)).fetchone()
    if not r:
        return {"status": "missing"}
    gate_prompt = ("You are the operator's comms gate. Given this inbound message, does it warrant a reply that "
                   "carries info or a decision (not a pure 'thanks/got it', not a closed thread)? "
                   f"Inbound: {r[3]}\nAnswer JSON {{reply_needed:bool, reason:str}}.")
    try:
        verdict = BR.judge(gate_prompt, schema={"type": "object", "additionalProperties": False,
                "properties": {"reply_needed": {"type": "boolean"}, "reason": {"type": "string"}},
                "required": ["reply_needed", "reason"]}, effort="medium")
    except BR.BrainUnavailable as e:
        return {"status": "brain_unavailable", "detail": str(e)[:120]}  # fail-closed: stays pending_gate
    status = apply_gate(conn, draft_id, verdict["reply_needed"], verdict["reason"])
    if status == "skipped_no_reply":
        return {"status": "skipped_no_reply"}
    # draft in the operator's voice (style rules live in the prompt/skill the agentic path applies)
    draft_prompt = ("Draft a reply in the operator's voice (direct, warm, no fluff, no em-dashes, no AI filler). "
                    f"Inbound: {r[3]}\nReturn only the reply text.")
    try:
        draft = BR.judge(draft_prompt, effort="high")
    except BR.BrainUnavailable:
        return {"status": "gated_no_brain_for_draft"}  # stays pending_draft
    apply_draft(conn, draft_id, draft)
    return {"status": "draft"}


# ───────────────────────── learn from the operator's tweaks ─────────────────────────
def _classify_diff(draft: str, final: str, sim: float) -> str:
    if final and draft and len(final) < 0.7 * len(draft):
        return "shortened"
    if (draft.splitlines()[:1] or [""])[0].strip().lower() != (final.splitlines()[:1] or [""])[0].strip().lower():
        return "greeting"
    if sim < 0.5:
        return "rewrite"
    return "style"


def capture_tweak(conn, draft_id: int, final_text: str) -> dict:
    """When the operator's actually-sent reply differs from the stored draft, record the
    diff in comms_draft_outcomes and STAGE one learning proposal through the steward (never
    a direct learnings write). Returns {'changed', 'outcome_id', 'staged_learning'}."""
    import difflib
    import steward_staging as S
    row = conn.execute("SELECT draft_text, channel FROM comms_drafts WHERE id=?", (draft_id,)).fetchone()
    if not row:
        return {"changed": False, "error": "no_such_draft"}
    prior = conn.execute("SELECT id FROM comms_draft_outcomes WHERE draft_id=? ORDER BY id LIMIT 1", (draft_id,)).fetchone()
    if prior:  # idempotent: this draft's tweak was already captured
        return {"changed": False, "outcome_id": prior[0], "staged_learning": False, "dedup": True}
    draft_text, channel = (row[0] or ""), row[1]
    sim = difflib.SequenceMatcher(None, draft_text, final_text or "").ratio()
    if sim >= 0.995:  # effectively unchanged -> nothing to learn
        conn.execute("UPDATE comms_drafts SET status='sent_by_operator', updated_at=datetime('now') WHERE id=?", (draft_id,))
        conn.commit()
        return {"changed": False, "outcome_id": None, "staged_learning": False}
    category = _classify_diff(draft_text, final_text or "", sim)
    diff = list(difflib.unified_diff(draft_text.splitlines(), (final_text or "").splitlines(), lineterm="", n=1))
    diff_json = json.dumps({"similarity": round(sim, 3), "category": category,
                            "draft_len": len(draft_text), "final_len": len(final_text or ""),
                            "unified_diff": diff[:60]})
    cur = conn.execute("INSERT INTO comms_draft_outcomes(draft_id,final_text,diff_json,channel,category) VALUES(?,?,?,?,?)",
                       (draft_id, final_text, diff_json, channel, category))
    outcome_id = cur.lastrowid
    conn.execute("UPDATE comms_drafts SET status='sent_by_operator', updated_at=datetime('now') WHERE id=?", (draft_id,))
    conn.commit()
    # stage exactly ONE learning proposal (steward publishes it as status='proposed').
    staged = S.submit(
        conn, target_table="learnings",
        natural_key={"content_hash": f"comms_tweak:{outcome_id}"},
        payload={"title": f"Comms draft tweak ({category}) on {channel}",
                 "description": f"The operator edited a {channel} draft ({category}; similarity {sim:.2f}). "
                                f"The operator's edits are the ground truth; see comms_draft_outcomes #{outcome_id}.",
                 "apply_when": f"When drafting a {channel} reply, match the operator's edited style from comms_draft_outcomes (category={category}).",
                 "content_hash": f"comms_tweak:{outcome_id}", "memory_type": "proposal", "status": "proposed",
                 "source": "comms_monitor:learn_from_tweaks"},
        submitted_by="comms_monitor", source_table="comms_draft_outcomes", source_id=str(outcome_id))
    return {"changed": True, "outcome_id": outcome_id, "staged_learning": True, "staging_id": staged["staging_id"], "category": category}


# ───────────────────────── learn-from-edits: draft ↔ sent matching ─────────────────────────
# The draft the operator actually sends is the ground truth. We match a stored draft to
# the SENT email that answered the same thread/recipient, edit-distance-bucket the two, and
# record ONE comms_draft_outcomes row per draft. capture_tweak() (above) is the per-chat path
# used when reconcile knows the final text; the functions below are the bulk/backfill path and
# the go-forward sync sweep, and they carry the extra bucket/ratio/matched_email_id columns.

# Buckets keyed off the edit-distance ratio (draft vs sent, quotes stripped):
#   kept (>=0.95) | light_edit (0.80-0.95) | heavy_edit (0.50-0.80) | discarded (<0.50, a send exists)
# unsent = no matching sent mail at all (or no draft text / non-email channel).
_OUTCOME_MATCH_PRESLACK_DAYS = 3    # a batch-late draft row can post-date the operator's send by a bit
_OUTCOME_MATCH_WINDOW_DAYS = 21     # cap how far after the draft we'll accept a reply as "the send"

try:
    from rapidfuzz import fuzz as _rf_fuzz  # type: ignore
except Exception:  # noqa: BLE001
    _rf_fuzz = None


def _norm_for_ratio(text: str) -> str:
    """Whitespace-collapsed, CR-stripped text for a stable edit-distance compare."""
    return " ".join((text or "").replace("\r\n", "\n").split())


def _edit_ratio(draft: str, final: str) -> tuple[float, bool]:
    """Return (ratio in [0,1], used_rapidfuzz). rapidfuzz if importable, else difflib."""
    a, b = _norm_for_ratio(draft), _norm_for_ratio(final)
    if not a or not b:
        return 0.0, False
    if _rf_fuzz is not None:
        return _rf_fuzz.ratio(a, b) / 100.0, True
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio(), False


def bucket_for_ratio(ratio: float) -> str:
    if ratio >= 0.95:
        return "kept"
    if ratio >= 0.80:
        return "light_edit"
    if ratio >= 0.50:
        return "heavy_edit"
    return "discarded"


def match_sent_for_draft(conn, draft: dict) -> dict:
    """Find the SENT email that answered a draft's thread+recipient. Returns a dict with
    matched=True/False. When matched: email_id, final_text (quote-stripped sent body),
    ratio, used_rf, method. When not: method in {non_email_channel, no_draft_text, no_match}."""
    channel = (draft.get("channel") or "").lower()
    draft_text = draft.get("draft_text") or ""
    sender_email = draft.get("sender_email") or ""
    thread_id = draft.get("thread_id")
    created_at = draft.get("created_at")
    if channel != "gmail":
        return {"matched": False, "method": "non_email_channel"}
    if not draft_text.strip():
        return {"matched": False, "method": "no_draft_text"}
    if not (thread_id and sender_email and created_at):
        return {"matched": False, "method": "no_match"}
    rows = conn.execute(
        "SELECT id, timestamp, body FROM emails "
        "WHERE thread_id=? AND is_outgoing=1 "
        "  AND recipients_json LIKE '%'||?||'%' "
        "  AND timestamp >= datetime(?, ?) AND timestamp <= datetime(?, ?) "
        "ORDER BY timestamp",
        (thread_id, sender_email,
         created_at, f"-{_OUTCOME_MATCH_PRESLACK_DAYS} days",
         created_at, f"+{_OUTCOME_MATCH_WINDOW_DAYS} days"),
    ).fetchall()
    if not rows:
        return {"matched": False, "method": "no_match"}
    # Prefer the earliest send AT/AFTER the draft; else the latest send just before it.
    after = [r for r in rows if str(r[1]) >= str(created_at)]
    if after:
        chosen, method = after[0], "thread_recipient"
    else:
        chosen, method = rows[-1], "thread_recipient_preslack"
    email_id, _ts, body = chosen[0], chosen[1], chosen[2]
    final_text = _unquoted_body(body or "").strip()
    ratio, used_rf = _edit_ratio(draft_text, final_text)
    return {"matched": True, "email_id": email_id, "final_text": final_text,
            "ratio": ratio, "used_rf": used_rf, "method": method}


def record_outcome(conn, draft: dict, *, run_ts: str | None = None) -> dict:
    """Insert ONE comms_draft_outcomes row for a draft (idempotent: skip if it already has one).
    Does NOT stage a learning — pure capture, safe for bulk backfill and the sync sweep.
    Pass run_ts to stamp captured_at explicitly (so a reversal by captured_at is exact)."""
    draft_id = draft["id"]
    if conn.execute("SELECT 1 FROM comms_draft_outcomes WHERE draft_id=?", (draft_id,)).fetchone():
        return {"draft_id": draft_id, "inserted": False, "reason": "already_captured"}
    draft_text = draft.get("draft_text") or ""
    channel = draft.get("channel")
    m = match_sent_for_draft(conn, draft)
    if m["matched"]:
        ratio = round(m["ratio"], 4)
        bucket = bucket_for_ratio(ratio)
        final_text = m["final_text"]
        matched_email_id = m["email_id"]
        method = m["method"]
        category = _classify_diff(draft_text, final_text, ratio)
        diff_json = json.dumps({"ratio": ratio, "bucket": bucket, "method": method,
                                "matched_email_id": matched_email_id, "used_rapidfuzz": m["used_rf"],
                                "draft_len": len(draft_text), "final_len": len(final_text)})
    else:
        ratio = None
        bucket = "unsent"
        final_text = None
        matched_email_id = None
        method = m["method"]
        category = None
        diff_json = json.dumps({"bucket": bucket, "method": method, "draft_len": len(draft_text)})
    cur = conn.execute(
        "INSERT INTO comms_draft_outcomes"
        "(draft_id, final_text, diff_json, channel, category, bucket, ratio, match_method, matched_email_id, captured_at) "
        "VALUES(?,?,?,?,?,?,?,?,?, COALESCE(?, CURRENT_TIMESTAMP))",
        (draft_id, final_text, diff_json, channel, category, bucket, ratio, method, matched_email_id, run_ts),
    )
    return {"draft_id": draft_id, "inserted": True, "outcome_id": cur.lastrowid,
            "bucket": bucket, "ratio": ratio, "method": method}


def capture_new_outcomes(conn=None, limit: int = 500) -> dict:
    """Go-forward sync sweep: fill comms_draft_outcomes for drafts that don't have an outcome
    row yet. Called at the end of brief.py sync, right after reconcile_chat_replies, so newly
    landed SENT mail gets diffed against the draft it answered. Idempotent + capped.
    Opens+closes its own connection when none is passed (the sync call site)."""
    own = conn is None
    if own:
        conn = connect()
    try:
        return _capture_new_outcomes(conn, limit)
    finally:
        if own:
            conn.close()


def _capture_new_outcomes(conn, limit: int) -> dict:
    rows = conn.execute(
        "SELECT d.id, d.channel, d.thread_id, d.sender_email, d.draft_text, d.created_at "
        "FROM comms_drafts d "
        "LEFT JOIN comms_draft_outcomes o ON o.draft_id = d.id "
        "WHERE o.id IS NULL "
        "ORDER BY d.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    cols = ("id", "channel", "thread_id", "sender_email", "draft_text", "created_at")
    counts: dict[str, int] = {}
    inserted = 0
    for r in rows:
        res = record_outcome(conn, dict(zip(cols, r)))
        if res.get("inserted"):
            inserted += 1
            counts[res["bucket"]] = counts.get(res["bucket"], 0) + 1
    conn.commit()
    return {"scanned": len(rows), "inserted": inserted, "by_bucket": counts}


# There is no outbound-delivery function in this module, by design. The monitor only writes
# comms_drafts and notifies the operator; actually delivering a reply is always a human action.


def status() -> dict:
    conn = connect()
    try:
        h = conn.execute("SELECT last_tick_at,status,ticks_total,drafts_made FROM comms_monitor_health WHERE id=1").fetchone()
        return {"health": h,
                "pending_gate": conn.execute("SELECT COUNT(*) FROM comms_drafts WHERE status='pending_gate'").fetchone()[0],
                "pending_draft": conn.execute("SELECT COUNT(*) FROM comms_drafts WHERE status='pending_draft'").fetchone()[0],
                "ready_drafts": conn.execute("SELECT COUNT(*) FROM comms_drafts WHERE status='draft'").fetchone()[0]}
    finally:
        conn.close()


def reconcile_chat_replies(conn=None, dry_run=False):
    """Inbox-chat resolution: flip chat comms_drafts to 'superseded_handled' when the
    operator has since replied/reacted (detected by _already_handled). The chat counterpart
    of the email stuck-replied sweep -- a draft we no longer need to send because the thread
    is already answered. Covers discord + beeper. Returns the number flipped. Fail-open per
    row. Wire into brief.py so it runs each sync."""
    own = conn is None
    if own:
        conn = connect()
    flipped = 0
    try:
        rows = conn.execute(
            "SELECT id, channel, inbound_msg_id FROM comms_drafts "
            "WHERE channel IN ('discord','beeper') AND status IN ('draft','pending_gate','pending_draft') "
            "AND inbound_msg_id IS NOT NULL").fetchall()
        for did, ch, mid in rows:
            try:
                handled, reason = _already_handled(conn, {"channel": ch, "msg_id": mid})
            except Exception:
                handled, reason = False, ""
            if handled:
                if not dry_run:
                    conn.execute(
                        "UPDATE comms_drafts SET status='superseded_handled', updated_at=datetime('now'), "
                        "gate_reason=COALESCE(gate_reason,'')||' [reconcile_chat_replies:'||?||']' WHERE id=?",
                        (reason, did))
                flipped += 1
        if not dry_run:
            conn.commit()
    finally:
        if own:
            conn.close()
    return flipped


def init_cursors_to_now():
    """Initialize per-channel cursors to the current max id so the first real run does NOT
    backfill years of history -- it processes only genuinely new inbound."""
    conn = connect()
    for src, tbl in (("comms_monitor_gmail", "emails"), ("comms_monitor_discord", "discord_messages"),
                     ("comms_monitor_beeper", "beeper_messages")):
        mx = conn.execute(f"SELECT COALESCE(MAX(id),0) FROM {tbl}").fetchone()[0]
        _set_cursor(conn, src, mx)
    conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "tick":
        # Liveness heartbeat (fail-open; the monitor DRAFTS only, never sends).
        # job_heartbeat is optional: when absent the tick still runs, just without
        # a job-level heartbeat row.
        try:
            # NOTE: import under a DISTINCT name. Aliasing this to _heartbeat once
            # shadowed the module-level _heartbeat(conn, status, drafts_made=...)
            # def above and TypeError'd every tick.
            from job_heartbeat import heartbeat as _job_heartbeat
        except Exception:
            import contextlib as _cl
            def _job_heartbeat(job):
                return _cl.nullcontext(type("_HB", (), {"rows_touched": 0, "exit_note": None})())
        with _job_heartbeat("CommsMonitor") as _hb:
            _out = tick()
            try:
                _hb.rows_touched = int(_out.get("landed", 0) or 0) if isinstance(_out, dict) else 0
            except Exception:
                pass
            print(json.dumps(_out, indent=2, default=str))
    elif cmd == "init-cursors":
        init_cursors_to_now(); print("cursors initialized to now")
    elif cmd == "reconcile-chat":
        print(f"reconcile_chat_replies: flipped {reconcile_chat_replies(dry_run='--dry-run' in sys.argv)} chat drafts to superseded_handled")
    else:
        print(json.dumps(status(), indent=2, default=str))
