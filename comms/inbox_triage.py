"""Inbox triage: classify email_threads into lanes + tier + SLA, surface breaches.

DB-side companion to a lane-based mail organization. Deterministic rule-based
classifier, no LLM. Source of truth for whether a thread "needs reply" is
`email_threads.status='pending'` AND inbound newer than outbound AND
age > freshness_sla_hours.

Configuration lives in config.toml (see config.example.toml). Top-level keys
used: [operator] emails, [org] domain, [contacts] vip. Everything else is under
[triage]: shared_inbox_groups, delegated_groups, vip_blast_prefixes,
blast_sender, blast_subject_prefixes, payment_humans, payment_auto,
payment_vendor_names, partner_emails, partner_domains, priority_roles,
general_roles, low_signal_domains, calendar_domains, auto_close_domains,
rewrite_automation_names, mention_tokens, delegated_escalation_keywords,
imminent_window_days. All lists default empty: unconfigured rules simply never
fire and the classifier degrades to the generic sender-domain heuristics.

Optional tables (feature-detected, none ship with this kit):
  partner_orgs  (contact_email, person_id)          -> partner lane seeding
  person_roles  (person_id, role, context_slug,
                 updated_at, inserted_at)           -> role-based lanes
  events        (slug, start_date, end_date)        -> imminent-event stage boost

Usage:
    inbox_triage.py classify [--all|--missing|--recent|--thread <id>|--person <id>] [--dry-run]
    inbox_triage.py backfill [--days N]      # populate email_threads for any inbound thread missing a row
    inbox_triage.py reconcile-replies        # auto-close false-positive open AIs
    inbox_triage.py status                   # lane breakdown + SLA breaches
    inbox_triage.py breaches [--lane L]      # only breached threads, oldest first
    inbox_triage.py daily                    # JSON output for brief.py / kanban
    inbox_triage.py classify-new             # incremental: only new threads since last run
    inbox_triage.py auto-close-stale         # post-hoc close pass
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import _db  # unified connector (busy_timeout + FK ON)
import config
import paths
from audit_actor import set_actor, clear_actor

sys.stdout.reconfigure(encoding="utf-8")

DB = Path(str(paths.DB_PATH))


def _cfg_set(key: str) -> set[str]:
    """Lowercased set from a config list; empty when unconfigured."""
    return {str(v).strip().lower() for v in (config.get(key, None) or []) if str(v).strip()}


def _cfg_tuple(key: str) -> tuple[str, ...]:
    """Lowercased tuple from a config list; empty when unconfigured."""
    return tuple(str(v).strip().lower() for v in (config.get(key, None) or []) if str(v).strip())


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """Feature-detect a table/view; optional tables degrade gracefully."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (name,)).fetchone()
    return row is not None


# ── Lane constants ──────────────────────────────────────────────────────────

LANE_VIP = "vip"                    # the manager / hand-picked key contacts
LANE_PARTNER = "partner"            # external partner orgs
LANE_COLLABORATOR = "collaborator"  # people holding a configured priority role
LANE_ORG_INTERNAL = "org_internal"  # colleagues on the org domain
LANE_PAYMENT = "payment"
LANE_GENERAL = "general"            # default external correspondents
LANE_CALENDAR = "calendar"
LANE_LOW_SIGNAL = "low_signal"
LANE_SHARED_INBOX = "shared_inbox"

LANES_ORDERED = [
    LANE_VIP, LANE_PARTNER, LANE_COLLABORATOR, LANE_SHARED_INBOX,
    LANE_ORG_INTERNAL, LANE_PAYMENT, LANE_GENERAL, LANE_CALENDAR,
    LANE_LOW_SIGNAL,
]

# Tier defaults per lane (lower = higher priority)
LANE_TIER = {
    LANE_VIP: 0,
    LANE_PARTNER: 1,
    LANE_COLLABORATOR: 2,
    LANE_SHARED_INBOX: 2,
    LANE_ORG_INTERNAL: 2,
    LANE_PAYMENT: 1,
    LANE_GENERAL: 3,
    LANE_CALENDAR: 4,
    LANE_LOW_SIGNAL: 4,
}

# ── Operator configuration (config.toml) ────────────────────────────────────
# All example values below are FICTIONAL; fill in your own config.toml.

# Every address that counts as "ours": the operator's own inbox plus any
# shared alias the operator answers from. Keeping this complete matters: it
# drives the org-internal rule and the delegated-group escalation carve-out.
# e.g. emails = ["jane@example.org", "team@example.org"]
OUR_EMAILS = _cfg_set("operator_emails")

# VIP contacts (typically the manager): their threads take the vip lane
# (tier 0, tightest SLA). e.g. vip = ["director@example.org"]
VIP_CONTACTS = _cfg_set("vip_contacts")

# Subject prefixes that mark a VIP mass-send rather than a personal note
# (e.g. a weekly "Checking in" blast); those demote to low_signal.
VIP_BLAST_PREFIXES = _cfg_tuple("triage.vip_blast_prefixes")

# The org's email domain; colleague mail routes org_internal. Empty = rule off.
ORG_DOMAIN = str(config.get("org_domain") or "").strip().lower()

# Google-Group shared inboxes. Mail TO these lands in Gmail's Forums tab
# (CATEGORY_FORUMS) and never surfaces in Primary, which is how genuine
# human mail can sit buried for weeks. Listing them here gives that mail its
# own lane with a real SLA. e.g. ["support@example.org", "info@example.org"]
SHARED_INBOX_GROUPS = _cfg_tuple("triage.shared_inbox_groups")

# Delegated program Google-Groups: mail on these is OWNED by a specific
# teammate, not the operator. The operator is only subscribed to the group,
# so all of its coordination traffic (scheduling, invites, acceptance chatter)
# floods the inbox and mis-routes to partner/general lanes, manufacturing
# needs-reply pressure for work that isn't theirs. Such threads demote to
# low_signal UNLESS they're addressed to one of our own inboxes directly or
# the subject is a general-ops ask (money / official letter).
# e.g. { "fellowship@example.org" = "program-owner" }
DELEGATED_GROUPS = {
    str(k).strip().lower(): str(v).strip().lower()
    for k, v in (config.get("triage.delegated_groups", None) or {}).items()
}

# General-ops asks that pull a delegated-group thread back to the operator
# even while the owner handles the rest of the thread. Subject-only match
# (kept simple on purpose). Word-boundary exact match: include the variants
# you care about (e.g. both "reimburse" and "reimbursement").
_DELEGATED_ESCALATE_KEYWORDS = _cfg_tuple("triage.delegated_escalation_keywords") or (
    "payment", "stipend", "invoice", "reimburse", "reimbursement",
    "letter", "visa", "tax", "contract", "wire",
)
_DELEGATED_ESCALATE_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _DELEGATED_ESCALATE_KEYWORDS) + r")\b",
    re.I)


def _luma_masked_human(sender: str, sender_name: str | None) -> bool:
    """True when Luma relays a real person's message (host invites, attendee
    questions): sender is usr-x@user.luma-mail.com but sender_name is the
    human. Luma's own automation has 'Luma' (or no person name) as the name."""
    if not sender.endswith("@user.luma-mail.com"):
        return False
    name = (sender_name or "").strip()
    return bool(name) and " " in name and "luma" not in name.lower()


# Automation senders that arrive via Google-Group DMARC rewrites; they belong
# in low_signal (or payment for the payment vendor), never the shared-inbox
# human lane. Display-name tokens, lowercased. Extend via config.
_REWRITE_AUTOMATION = (
    "luma", "overleaf", "substack", "tally forms",
    "google forms", "airtable", "stripe",
) + _cfg_tuple("triage.rewrite_automation_names")

# Display-name tokens of the payment vendor's automation (seen through group
# rewrites); routes to the payment lane at low tier instead of low_signal.
# e.g. payment_vendor_names = ["acmepay"]
PAYMENT_VENDOR_NAMES = _cfg_tuple("triage.payment_vendor_names")

# Content-aware low-signal: an automated/monitoring subject is never a human
# obligation, even when it arrives from a human's address.
_AUTOMATED_NOISE_RE = re.compile(
    r"^(re:\s*)?(error[_\s]|\[auto\]|\[cron\]|cron[:\s]|lambda\b|monitoring\b|"
    r"alarm[:\s]|alert[:\s]|automated\b|noreply|no-reply|build (failed|succeeded)|"
    r"deployment\b|uptime\b|healthcheck\b)", re.I)


def _demask_group_rewrite(sender: str, sender_name: str | None) -> tuple[bool, str]:
    """Detect Google-Group DMARC From-rewrites: sender is one of OUR group
    addresses but display name is \"'Original Sender' via <Group>\".
    Returns (is_rewrite, original_name)."""
    name = (sender_name or "")
    if sender not in SHARED_INBOX_GROUPS or "' via " not in name:
        return False, ""
    orig = name.split("' via ", 1)[0].lstrip("'").strip()
    return True, orig

# Freshness SLA in hours. NULL → no alarm.
TIER_SLA_HOURS = {
    0: 4,
    1: 24,
    2: 24,
    3: 72,
    4: None,
}

# Lane-level SLA overrides (hours). org_internal gets 48h, but it shares
# tier 2 with collaborator/shared_inbox (24h), so tier alone can't express it.
# Consulted in Classifier._make() after the tier SLA lookup; only applies at
# the lane's un-boosted tier, so stage-boosted threads keep the tighter SLA.
LANE_SLA_OVERRIDE = {
    LANE_ORG_INTERNAL: 48,
}

# ── Sender categorization ───────────────────────────────────────────────────

# Registration-confirmation / signup blasts sent from an org automation
# address are automation, not colleague mail. Without this rule they fall to
# the org-internal rule and its 48h SLA, producing hundreds of fake breaches.
# Matched on sender + subject prefix so a *human* writing from that address
# about anything else still routes org_internal.
# e.g. blast_sender = "no-reply@example.org"
#      blast_subject_prefixes = ["welcome aboard", "thank you for signing up"]
BLAST_SENDER = str(config.get("triage.blast_sender", "") or "").strip().lower()
BLAST_SUBJECT_PREFIXES = _cfg_tuple("triage.blast_subject_prefixes")

# Built-in generic noise senders; extend via [triage] low_signal_domains.
LOW_SIGNAL_DOMAINS = {
    "linkedin.com", "mailer-daemon", "no-reply", "noreply", "bounces",
    "convertkit.com", "kit.com", "substack.com", "patreon.com",
    "newsletter", "mailchimp.com",
    "github.com", "facebookmail.com", "slack.com", "loom.com",
    "atlassian.net", "billing", "stripe.com",
    "groupupdates@", "notifications-noreply@", "no-reply@", "noreply@",
    "user.luma-mail.com",
} | _cfg_set("triage.low_signal_domains")

CALENDAR_DOMAINS = {
    "luma-mail.com", "luma.com", "calendar.luma-mail.com",
    "zoom.us", "google.com/calendar", "calendar.google.com",
    "calendar-notification@google.com", "granola.ai",
} | _cfg_set("triage.calendar_domains")

# Humans at the payment vendor — high-tier payment lane.
# e.g. payment_humans = ["accounts@payvendor.example"]
PAYMENT_HUMANS = _cfg_set("triage.payment_humans")
# Automated payment senders — lower tier.
# e.g. payment_auto = ["jira@payvendor.example", "billing@zoom.us"]
PAYMENT_AUTO = _cfg_set("triage.payment_auto")

# Partner lane seed lists (see also the optional partner_orgs table).
# e.g. partner_emails = ["contact@partnerorg.example"]
#      partner_domains = ["partnerorg.example"]
PARTNER_EMAILS = _cfg_set("triage.partner_emails")
PARTNER_DOMAINS = _cfg_set("triage.partner_domains")

# person_roles role values that map to the collaborator lane (priority
# contacts) vs. the general lane. Empty = role lanes off.
# e.g. priority_roles = ["advisor", "reviewer", "mentor"]
#      general_roles = ["participant"]
PRIORITY_ROLES = _cfg_set("triage.priority_roles")
GENERAL_ROLES = _cfg_set("triage.general_roles")

# Body tokens that mark a direct mention of the operator (blocks auto-close of
# short messages). Defaults to "@<first name>" when [operator] name is set.
# e.g. mention_tokens = ["@Jane"]
_mention_cfg = [str(t) for t in (config.get("triage.mention_tokens", None) or []) if str(t).strip()]
if not _mention_cfg:
    _op_name = str(config.get("operator_name") or "").strip()
    _mention_cfg = ["@" + _op_name.split()[0]] if _op_name else []
MENTION_TOKENS = tuple(_mention_cfg)


# Cached partner emails + domains — config lists plus (when present) an
# operator-maintained partner_orgs table. This kit ships no such table;
# without it the partner lane runs on the config lists alone.
def _load_partner_set(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
    emails: set[str] = set(PARTNER_EMAILS)
    domains: set[str] = set(PARTNER_DOMAINS)
    for e in list(emails):
        if "@" in e:
            domains.add(e.split("@", 1)[1])
    if not _table_exists(conn, "partner_orgs"):
        return emails, domains
    try:
        rows = conn.execute("""
            SELECT contact_email FROM partner_orgs
             WHERE contact_email IS NOT NULL AND contact_email != ''
        """).fetchall()
        for (e,) in rows:
            e = e.strip().lower()
            if e:
                emails.add(e)
                if "@" in e:
                    domains.add(e.split("@", 1)[1])
        # Also pull all person_emails for any person tied to a partner_orgs row
        rows = conn.execute("""
            SELECT pe.email
              FROM person_emails pe
              JOIN partner_orgs po ON po.person_id = pe.person_id
             WHERE pe.email IS NOT NULL AND pe.email != ''
        """).fetchall()
        for (e,) in rows:
            e = e.strip().lower()
            if e:
                emails.add(e)
                if "@" in e:
                    domains.add(e.split("@", 1)[1])
    except sqlite3.OperationalError:
        pass  # schema mismatch on the optional table: config lists still apply
    return emails, domains


# ── Event stage boost ───────────────────────────────────────────────────────

def _load_imminent_events(conn: sqlite3.Connection, window_days: int | None = None) -> set[str]:
    """Return slug set for events starting within window_days from now (or
    currently active). Reads an optional `events` table (slug, start_date,
    end_date); this kit ships none, so the stage boost degrades to never
    firing until the operator creates one."""
    if window_days is None:
        try:
            window_days = int(config.get("triage.imminent_window_days", 21) or 21)
        except (TypeError, ValueError):
            window_days = 21
    if not _table_exists(conn, "events"):
        return set()
    try:
        rows = conn.execute("""
            SELECT slug FROM events
             WHERE end_date >= date('now', '-3 days')
               AND start_date <= date('now', ?)
        """, (f"+{window_days} days",)).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {r[0] for r in rows}


# ── Classifier ──────────────────────────────────────────────────────────────

class Classifier:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.partner_emails, self.partner_domains = _load_partner_set(conn)
        self.imminent_events = _load_imminent_events(conn)
        # Pre-fetch person_id → role mapping from the optional person_roles
        # table. Ordered by updated_at DESC so the LATEST role wins (e.g. a
        # general contact promoted to a priority role gets classified as such
        # from that point on). Absent table = empty cache = role lanes off.
        self._person_roles_cache: dict[int, list[tuple[str, str | None]]] = {}
        if _table_exists(conn, "person_roles"):
            try:
                rows = conn.execute("""
                    SELECT person_id, role, context_slug
                      FROM person_roles
                     ORDER BY COALESCE(updated_at, inserted_at) DESC
                """).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for row in rows:
                self._person_roles_cache.setdefault(row[0], []).append(
                    ((row[1] or "").lower(), row[2]))

    def classify_thread(self, last_sender_email: str | None, subject: str | None,
                        person_id: int | None, context_slug: str | None,
                        group_recipient: bool = False,
                        last_sender_name: str | None = None,
                        delegated_owner: str | None = None,
                        addressed_to_us: bool = False) -> dict:
        """Return {lane, tier_base, stage_boost, tier, sla_hours, source}.

        group_recipient: latest inbound message was addressed to a shared
        Google-Group inbox (SHARED_INBOX_GROUPS). last_sender_name de-masks
        Luma-relayed humans out of the low_signal/calendar domain rules.
        delegated_owner: a DELEGATED_GROUPS alias is a recipient (thread owned
        by that teammate, not the operator). addressed_to_us: one of
        OUR_EMAILS is a direct recipient (the escalation carve-out for
        delegated groups).
        """
        sender = (last_sender_email or "").strip().lower()
        subj = (subject or "")

        # Rule 0: content-aware low-signal. An automated/monitoring subject
        # ("Error_...", cron/alarm/build/deploy noise) is never a human
        # obligation, so it routes to low_signal regardless of sender -- even a
        # VIP's address (an alarm CC'd from their account is still noise).
        if subj.startswith("Error_") or _AUTOMATED_NOISE_RE.match(subj.strip()):
            return self._make(LANE_LOW_SIGNAL, "rule:automated_noise_subject", context_slug)

        # Rule 0b: registration-confirmation blasts from the org automation address.
        if (BLAST_SENDER and sender == BLAST_SENDER and BLAST_SUBJECT_PREFIXES
                and subj.strip().lower().startswith(BLAST_SUBJECT_PREFIXES)):
            return self._make(LANE_LOW_SIGNAL, "rule:blast_sender", context_slug)

        # Rule 1: VIP contacts (excluding their mass-send subjects)
        if sender in VIP_CONTACTS:
            if VIP_BLAST_PREFIXES and subj.lower().startswith(VIP_BLAST_PREFIXES):
                return self._make(LANE_LOW_SIGNAL, "rule:vip_blast", context_slug)
            return self._make(LANE_VIP, "rule:vip", context_slug)

        # De-mask: Google-Group DMARC rewrite ("'X' via Group" from our own
        # group address). Route by who X actually is, not by the group alias.
        rewritten, orig_name = _demask_group_rewrite(sender, last_sender_name)
        if rewritten:
            low_orig = orig_name.lower()
            if any(b in low_orig for b in _REWRITE_AUTOMATION) or any(
                    v in low_orig for v in PAYMENT_VENDOR_NAMES):
                if any(v in low_orig for v in PAYMENT_VENDOR_NAMES):
                    return self._make(LANE_PAYMENT, "rule:rewrite_payment_auto",
                                      context_slug, tier_override=3)
                return self._make(LANE_LOW_SIGNAL, "rule:rewrite_automation", context_slug)
            return self._make(LANE_SHARED_INBOX, "rule:shared_inbox_via_demask", context_slug)

        # De-mask: a human writing through Luma's relay must not fall into the
        # low_signal/calendar domain buckets like Luma's own automation does.
        luma_human = _luma_masked_human(sender, last_sender_name)

        # Rule 2: Low signal
        domain = sender.split("@", 1)[1] if "@" in sender else ""
        if not luma_human and self._domain_match(sender, domain, LOW_SIGNAL_DOMAINS):
            return self._make(LANE_LOW_SIGNAL, "rule:low_signal", context_slug)

        # Rule 3: Calendar
        if not luma_human and self._domain_match(sender, domain, CALENDAR_DOMAINS):
            return self._make(LANE_CALENDAR, "rule:calendar", context_slug)

        # Rule 4: Payment (humans high tier)
        if sender in PAYMENT_HUMANS:
            return self._make(LANE_PAYMENT, "rule:payment_human", context_slug, tier_override=1)
        # Rule 5: Payment (automated, low tier)
        if sender in PAYMENT_AUTO:
            return self._make(LANE_PAYMENT, "rule:payment_auto", context_slug, tier_override=3)

        # Rule 5b: Delegated program group (a teammate owns it). Not addressed
        # to one of our inboxes directly and not a money/letter ask → the
        # program owner's obligation, not the operator's. Demote to low_signal
        # (auto-closes at ingest) so it stops surfacing as needs-reply. Placed
        # after VIP/automation/payment so those still win; before
        # partner/general, which is what used to mis-catch it.
        if (delegated_owner and not addressed_to_us
                and not _DELEGATED_ESCALATE_RE.search(subj)):
            return self._make(LANE_LOW_SIGNAL, f"rule:delegated_{delegated_owner}",
                              context_slug)

        # Rule 6: Partner (exact email or domain match against the partner set)
        if sender in self.partner_emails or domain in self.partner_domains:
            return self._make(LANE_PARTNER, "rule:partner_org", context_slug)

        # Rule 7: Role-based lanes — LATEST role wins. `roles` is pre-sorted
        # by updated_at DESC; first entry is the most recent role for this
        # person. If the most recent role is a priority role they route to
        # collaborator even if older general-role rows exist.
        if person_id is not None and person_id in self._person_roles_cache:
            roles = self._person_roles_cache[person_id]
            latest_role, latest_ctx = roles[0]
            if latest_role in PRIORITY_ROLES:
                stage = 1 if (latest_ctx and latest_ctx in self.imminent_events) else 0
                return self._make(LANE_COLLABORATOR, "rule:person_role_priority",
                                  context_slug, stage_boost=stage)
            if latest_role in GENERAL_ROLES:
                stage = 1 if (latest_ctx and latest_ctx in self.imminent_events) else 0
                return self._make(LANE_GENERAL, "rule:person_role_general",
                                  context_slug, stage_boost=stage)

        # Rule 8: Shared-inbox — external mail addressed to a Google-Group
        # inbox that no higher-priority rule claimed. Tier 2 so it surfaces
        # with a 24h SLA instead of drowning in Forums-tab burial.
        if group_recipient and (not ORG_DOMAIN or domain != ORG_DOMAIN):
            src = "rule:shared_inbox_luma_demask" if luma_human else "rule:shared_inbox_group"
            return self._make(LANE_SHARED_INBOX, src, context_slug)

        # Rule 9: Org internal (colleagues on the org domain)
        if ORG_DOMAIN and domain == ORG_DOMAIN and sender not in OUR_EMAILS \
                and sender not in VIP_CONTACTS:
            return self._make(LANE_ORG_INTERNAL, "rule:org_internal", context_slug)

        # Rule 10: Default — external person, assume general correspondence
        return self._make(LANE_GENERAL, "rule:default_external", context_slug)

    def _domain_match(self, sender: str, domain: str, candidates: set[str]) -> bool:
        if sender in candidates:
            return True
        for c in candidates:
            if "@" in c:
                if sender == c or sender.endswith(c):
                    return True
            else:
                if domain == c or domain.endswith("." + c) or c in sender:
                    return True
        return False

    def _make(self, lane: str, source: str, context_slug: str | None,
              tier_override: int | None = None, stage_boost: int = 0) -> dict:
        tier_base = tier_override if tier_override is not None else LANE_TIER[lane]
        # Event stage boost — only applies to lanes with a tier numeric SLA
        if context_slug and context_slug in self.imminent_events and tier_base > 0:
            stage_boost = max(stage_boost, 1)
        tier = max(0, tier_base - stage_boost)
        sla = TIER_SLA_HOURS[tier]
        # Lane SLA override (see LANE_SLA_OVERRIDE). Skip when a stage boost
        # tightened the tier: imminent-event threads keep the faster SLA.
        if tier == tier_base and lane in LANE_SLA_OVERRIDE:
            sla = LANE_SLA_OVERRIDE[lane]
        return {
            "lane": lane,
            "tier_base": tier_base,
            "stage_boost": stage_boost,
            "tier": tier,
            "sla_hours": sla,
            "source": source,
        }


# ── Auto-close helpers ──────────────────────────────────────────────────────
# Stops email_threads from re-accumulating junk after a mass cleanup.

_AUTO_CLOSE_DOMAINS = frozenset({
    "notion.so", "slack.com", "luma-mail.com",
    "meetup.com", "docs.google.com", "calendar.google.com",
    "drive.google.com",
} | _cfg_set("triage.auto_close_domains"))
_AUTO_CLOSE_LOCAL_PREFIXES = (
    "noreply", "notifications", "communications", "support",
    "do-not-reply", "do_not_reply",
)
_AUTO_CLOSE_SUBJECT_PREFIXES = (
    "[noreply]", "[automated]", "Receipt:", "Reminder:",
)


def is_auto_close_candidate(
    thread_row: dict, last_inbound_body: str | None = None
) -> tuple[bool, str | None]:
    """Return (should_close, reason). True means auto-close as no_action_needed."""
    lane = (thread_row.get("lane") or "").lower()
    if lane in (LANE_LOW_SIGNAL, LANE_CALENDAR):
        return True, f"lane:{lane}"
    sender = (thread_row.get("last_sender_email") or "").lower()
    if "@" in sender:
        local, _, domain = sender.partition("@")
        if domain in _AUTO_CLOSE_DOMAINS:
            return True, f"domain:{domain}"
        if any(local.startswith(p) for p in _AUTO_CLOSE_LOCAL_PREFIXES):
            return True, f"local:{local[:20]}"
    subj = thread_row.get("subject") or ""
    for prefix in _AUTO_CLOSE_SUBJECT_PREFIXES:
        if subj.startswith(prefix):
            return True, f"subject:{prefix}"
    if (
        last_inbound_body
        and len(last_inbound_body.strip()) < 20
        and "?" not in last_inbound_body
        and not any(t in last_inbound_body for t in MENTION_TOKENS)
    ):
        return True, "short_no_question"
    return False, None


def auto_close_stale(dry_run: bool = False) -> dict:
    """Post-hoc auto-close pass.

    Rules:
      0) reopen flip: status IN ('replied','stale') AND lane NOT IN
         ('low_signal','calendar')
         AND last_inbound_ts > last_outbound_ts
         AND last_inbound_ts > last_status_change_at -> status='pending'
         (they answered back AFTER we closed; needs attention again)
      1) replied flip: status='pending' AND last_outbound_ts > last_inbound_ts
         -> status='replied' (we sent the last word and nothing came back)
      2) general stale: status='pending' AND lane='general'
         AND last_inbound_ts < now - 30d -> status='stale'

    Returns {'reopened': N, 'replied_flipped': N, 'stale_flipped': N}.
    """
    _REOPEN_WHERE = f"""
         WHERE status IN ('replied','stale')
           AND COALESCE(lane,'') NOT IN ('{LANE_LOW_SIGNAL}','{LANE_CALENDAR}')
           AND last_inbound_ts IS NOT NULL
           AND last_outbound_ts IS NOT NULL
           AND last_inbound_ts > last_outbound_ts
           AND last_inbound_ts > COALESCE(last_status_change_at,'0000-01-01')
    """
    conn = connect()
    cur = conn.cursor()
    if dry_run:
        reopened = cur.execute(
            "SELECT COUNT(*) FROM email_threads" + _REOPEN_WHERE
        ).fetchone()[0]
        replied = cur.execute("""
            SELECT COUNT(*) FROM email_threads
             WHERE status='pending'
               AND last_outbound_ts IS NOT NULL
               AND last_inbound_ts IS NOT NULL
               AND last_outbound_ts > last_inbound_ts
        """).fetchone()[0]
        stale = cur.execute("""
            SELECT COUNT(*) FROM email_threads
             WHERE status='pending' AND lane=?
               AND last_inbound_ts IS NOT NULL
               AND last_inbound_ts < datetime('now','-30 days')
        """, (LANE_GENERAL,)).fetchone()[0]
        conn.close()
        return {"reopened": reopened, "replied_flipped": replied,
                "stale_flipped": stale, "dry_run": True}

    # Status flips cascade into the entities mirror (email_threads_to_entities_au);
    # the blocking write gate aborts them without an actor on this connection.
    set_actor(conn, "inbox_triage:auto-close-stale")
    cur.execute("""
        UPDATE email_threads
           SET status='pending',
               resolved_at=NULL,
               resolution_note=COALESCE(resolution_note,'')
                   || ' [reopen: inbound newer than outbound+close at ' || datetime('now') || ']',
               last_status_change_at=datetime('now')
    """ + _REOPEN_WHERE)
    reopened_n = cur.rowcount
    cur.execute("""
        UPDATE email_threads
           SET status='replied',
               resolution_note=COALESCE(resolution_note,'')
                   || ' [auto-close-stale: outbound>inbound at ' || datetime('now') || ']',
               last_status_change_at=datetime('now')
         WHERE status='pending'
           AND last_outbound_ts IS NOT NULL
           AND last_inbound_ts IS NOT NULL
           AND last_outbound_ts > last_inbound_ts
    """)
    replied_n = cur.rowcount
    cur.execute("""
        UPDATE email_threads
           SET status='stale',
               resolution_note=COALESCE(resolution_note,'')
                   || ' [auto-close-stale: general idle >30d at ' || datetime('now') || ']',
               resolved_at=datetime('now'),
               last_status_change_at=datetime('now')
         WHERE status='pending' AND lane=?
           AND last_inbound_ts IS NOT NULL
           AND last_inbound_ts < datetime('now','-30 days')
    """, (LANE_GENERAL,))
    stale_n = cur.rowcount
    clear_actor(conn)
    conn.commit()
    conn.close()
    return {"reopened": reopened_n, "replied_flipped": replied_n,
            "stale_flipped": stale_n, "dry_run": False}


# ── Operations ──────────────────────────────────────────────────────────────

def connect() -> sqlite3.Connection:
    conn = _db.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def backfill(days: int = 365, verbose: bool = False) -> int:
    """Populate email_threads for any inbound thread missing a row."""
    conn = connect()
    cur = conn.cursor()

    missing = cur.execute("""
        SELECT thread_id,
               MAX(subject) AS subject,
               MIN(timestamp) AS first_ts,
               MAX(CASE WHEN is_outgoing=0 THEN timestamp END) AS last_in,
               MAX(CASE WHEN is_outgoing=1 THEN timestamp END) AS last_out,
               SUM(CASE WHEN is_outgoing=0 THEN 1 ELSE 0 END) AS in_n,
               SUM(CASE WHEN is_outgoing=1 THEN 1 ELSE 0 END) AS out_n,
               COUNT(*) AS msg_n,
               MAX(person_id) AS person_id,
               (SELECT sender_email FROM emails e2
                 WHERE e2.thread_id = e.thread_id AND e2.is_outgoing=0
                 ORDER BY e2.timestamp DESC LIMIT 1) AS last_sender_in,
               (SELECT sender_email FROM emails e2
                 WHERE e2.thread_id = e.thread_id
                 ORDER BY e2.timestamp DESC LIMIT 1) AS last_sender_any
          FROM emails e
         WHERE timestamp >= datetime('now', ?)
           AND thread_id IS NOT NULL
           AND thread_id != ''
           AND thread_id NOT IN (SELECT thread_id FROM email_threads WHERE thread_id IS NOT NULL)
         GROUP BY thread_id
    """, (f"-{days} days",)).fetchall()

    inserted = 0
    # Attribute the email_threads inserts (and the entities/edges the AI trigger
    # spawns) so they don't land as NULL-actor cdc rows. conn-scoped, cleared below.
    set_actor(conn, "inbox_triage:thread-backfill")
    for r in missing:
        last_in = r["last_in"]
        last_out = r["last_out"]
        last_sender = r["last_sender_in"] or r["last_sender_any"]
        # Determine initial status
        if not last_in:
            status = "no_inbound"
        elif last_out and last_out > last_in:
            status = "replied"
        else:
            status = "pending"
        cur.execute("""
            INSERT OR IGNORE INTO email_threads
              (thread_id, subject, first_ts, last_inbound_ts, last_outbound_ts,
               last_sender_email, person_id, status, message_count, inbound_count,
               outbound_count, inserted_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        """, (r["thread_id"], r["subject"], r["first_ts"], last_in, last_out,
              last_sender, r["person_id"], status, r["msg_n"], r["in_n"], r["out_n"]))
        inserted += cur.rowcount
        if verbose and inserted % 100 == 0:
            print(f"  backfilled {inserted}/{len(missing)}")
    clear_actor(conn)
    conn.commit()
    conn.close()
    return inserted


def refresh_thread_cache(conn=None, days: int | None = 14, verbose: bool = False) -> int:
    """Re-derive email_threads freshness columns from the raw emails table.

    Self-healing: the cache columns (last_inbound_ts / last_outbound_ts /
    last_sender_email / counts) historically had exactly one incremental
    updater (the sync pipeline's post-merge refresh) and it was wall-clock
    gated, so missed threads stayed stale forever and every consumer
    (reconcile_replies, v_inbox_breaches, daily_payload, kanban) trusted the
    stale cache. This re-derives the columns for every thread with any email
    in the last `days` days; called first in classify_new() so every sync
    self-heals. Idempotent; safe at any cadence.

    Returns the number of threads whose columns actually changed.
    """
    own = conn is None
    if own:
        conn = connect()
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT e.thread_id,
               MAX(CASE WHEN is_outgoing=0 AND (labels IS NULL OR labels NOT LIKE '%DRAFT%') THEN timestamp END) AS last_in,
               MAX(CASE WHEN is_outgoing=1 AND (labels IS NULL OR labels NOT LIKE '%DRAFT%') THEN timestamp END) AS last_out,
               SUM(CASE WHEN is_outgoing=0 AND (labels IS NULL OR labels NOT LIKE '%DRAFT%') THEN 1 ELSE 0 END) AS in_n,
               SUM(CASE WHEN is_outgoing=1 AND (labels IS NULL OR labels NOT LIKE '%DRAFT%') THEN 1 ELSE 0 END) AS out_n,
               SUM(CASE WHEN labels IS NULL OR labels NOT LIKE '%DRAFT%' THEN 1 ELSE 0 END) AS msg_n,
               (SELECT sender_email FROM emails e2
                 WHERE e2.thread_id = e.thread_id AND e2.is_outgoing=0
                   AND (e2.labels IS NULL OR e2.labels NOT LIKE '%DRAFT%')
                 ORDER BY e2.timestamp DESC LIMIT 1) AS last_sender_in,
               (SELECT sender_email FROM emails e2
                 WHERE e2.thread_id = e.thread_id
                   AND (e2.labels IS NULL OR e2.labels NOT LIKE '%DRAFT%')
                 ORDER BY e2.timestamp DESC LIMIT 1) AS last_sender_any
          FROM emails e
         WHERE e.thread_id IN (
               SELECT DISTINCT thread_id FROM emails
                WHERE timestamp >= datetime('now', ?)
                  AND thread_id IS NOT NULL AND thread_id != '')
           AND e.thread_id IN (SELECT thread_id FROM email_threads)
         GROUP BY e.thread_id
    """, (f"-{days} days" if days else "-200 years",)).fetchall()
    # days=None/0 = full-history mode: the window filter degenerates to
    # always-true so EVERY thread's cache re-derives.
    changed = 0
    for r in rows:
        cur.execute("""
            UPDATE email_threads
               SET last_inbound_ts = ?, last_outbound_ts = ?,
                   inbound_count = ?, outbound_count = ?, message_count = ?,
                   last_sender_email = ?, updated_at = datetime('now')
             WHERE thread_id = ?
               AND (COALESCE(last_inbound_ts,'') != COALESCE(?,'')
                    OR COALESCE(last_outbound_ts,'') != COALESCE(?,'')
                    OR COALESCE(inbound_count,-1) != ?
                    OR COALESCE(outbound_count,-1) != ?
                    OR COALESCE(message_count,-1) != ?
                    OR COALESCE(last_sender_email,'') != COALESCE(?,''))
        """, (r["last_in"], r["last_out"], r["in_n"], r["out_n"], r["msg_n"],
              r["last_sender_in"] or r["last_sender_any"], r["thread_id"],
              r["last_in"], r["last_out"], r["in_n"], r["out_n"], r["msg_n"],
              r["last_sender_in"] or r["last_sender_any"]))
        changed += cur.rowcount
    conn.commit()
    if verbose:
        print(f"  refresh_thread_cache(days={days}): {len(rows)} threads scanned, {changed} repaired")
    if own:
        conn.close()
    return changed


def _recipient_meta(
    cur, thread_ids: list[str]
) -> dict[str, tuple[bool, str | None, str | None, bool]]:
    """Per thread, derived from the latest inbound message:
      (group_recipient, sender_name, delegated_owner, addressed_to_us)

    - group_recipient: addressed to a SHARED_INBOX_GROUPS alias
    - delegated_owner: a DELEGATED_GROUPS alias is a recipient → its owner slug
    - addressed_to_us: one of OUR_EMAILS is a direct to/cc recipient

    Batched over emails.recipients_json; one query per chunk, not one per thread.
    """
    if not thread_ids:
        return {}
    meta: dict[str, tuple[bool, str | None, str | None, bool]] = {}
    CHUNK = 500
    for i in range(0, len(thread_ids), CHUNK):
        chunk = thread_ids[i:i + CHUNK]
        ph = ",".join("?" * len(chunk))
        rows = cur.execute(f"""
            SELECT e.thread_id, e.sender_name, e.recipients_json
              FROM emails e
             WHERE e.thread_id IN ({ph})
               AND e.is_outgoing = 0
               AND e.timestamp = (SELECT MAX(e2.timestamp) FROM emails e2
                                   WHERE e2.thread_id = e.thread_id AND e2.is_outgoing = 0)
        """, tuple(chunk)).fetchall()
        for r in rows:
            rcpt = (r["recipients_json"] or "").lower()
            grp = any(g in rcpt for g in SHARED_INBOX_GROUPS)
            owner = next((own for addr, own in DELEGATED_GROUPS.items() if addr in rcpt), None)
            addressed_to_us = any(u in rcpt for u in OUR_EMAILS)
            meta[r["thread_id"]] = (grp, r["sender_name"], owner, addressed_to_us)
    return meta


def classify(scope: str = "all", thread_id: str | None = None,
             person_id: int | None = None, days: int = 14,
             dry_run: bool = False, verbose: bool = False) -> int:
    """Classify rows in email_threads. scope: all|missing|thread|person|recent."""
    conn = connect()
    cls = Classifier(conn)
    cur = conn.cursor()

    if scope == "thread" and thread_id:
        rows = cur.execute("""
            SELECT thread_id, subject, last_sender_email, person_id, context_slug
              FROM email_threads WHERE thread_id = ?
        """, (thread_id,)).fetchall()
    elif scope == "person" and person_id is not None:
        rows = cur.execute("""
            SELECT thread_id, subject, last_sender_email, person_id, context_slug
              FROM email_threads WHERE person_id = ?
        """, (person_id,)).fetchall()
    elif scope == "missing":
        rows = cur.execute("""
            SELECT thread_id, subject, last_sender_email, person_id, context_slug
              FROM email_threads WHERE lane IS NULL
        """).fetchall()
    elif scope == "recent":
        rows = cur.execute("""
            SELECT thread_id, subject, last_sender_email, person_id, context_slug
              FROM email_threads
             WHERE COALESCE(last_inbound_ts, last_outbound_ts, first_ts) >= datetime('now', ?)
        """, (f"-{days} days",)).fetchall()
    else:
        rows = cur.execute("""
            SELECT thread_id, subject, last_sender_email, person_id, context_slug
              FROM email_threads
        """).fetchall()

    meta = _recipient_meta(cur, [r["thread_id"] for r in rows])
    n = 0
    for r in rows:
        grp, sname, downer, to_us = meta.get(r["thread_id"], (False, None, None, False))
        result = cls.classify_thread(
            r["last_sender_email"], r["subject"], r["person_id"], r["context_slug"],
            group_recipient=grp, last_sender_name=sname,
            delegated_owner=downer, addressed_to_us=to_us,
        )
        if verbose and n < 5:
            print(f"  {r['thread_id']} → {result['lane']} (tier {result['tier']}, {result['source']})")
        if not dry_run:
            cur.execute("""
                UPDATE email_threads
                   SET lane=?, tier_base=?, stage_boost=?, stakeholder_tier=?,
                       freshness_sla_hours=?, classification_source=?,
                       auto_classified_at=datetime('now'), updated_at=datetime('now')
                 WHERE thread_id=?
            """, (result["lane"], result["tier_base"], result["stage_boost"],
                  result["tier"], result["sla_hours"], result["source"],
                  r["thread_id"]))
        n += 1
        if verbose and n % 500 == 0:
            print(f"  classified {n}/{len(rows)}")
    if not dry_run:
        conn.commit()
    conn.close()
    return n


def reconcile_replies(dry_run: bool = False) -> dict:
    """True reconciliation loop: re-derive desired status for ALL non-terminal
    threads (pending/waiting/draft_pending), and re-open terminal threads that
    got new inbound after resolution (closes the thread-reopen gap;
    auto_close_stale already covers the replied/stale flavors)."""
    conn = connect()
    cur = conn.cursor()
    # Status flips + action_items closes cascade into the entities mirror; the
    # blocking write gate aborts NULL-actor writes. set/clear nets out in dry_run.
    set_actor(conn, "inbox_triage:reconcile-replies")

    # 0) Reopen scan: terminal thread with inbound NEWER than both the
    #    resolution and our last outbound -> back to 'pending' (audit-noted).
    _REOPEN_TERMINAL = f"""
         WHERE status IN ('resolved','resolved_elsewhere','no_action_needed','info_captured')
           AND COALESCE(lane,'') NOT IN ('{LANE_LOW_SIGNAL}','{LANE_CALENDAR}')
           AND last_inbound_ts IS NOT NULL
           AND last_inbound_ts > COALESCE(last_outbound_ts,'0000-01-01')
           AND last_inbound_ts > COALESCE(last_status_change_at,'0000-01-01')
    """
    reopen_ids = [r[0] for r in cur.execute(
        "SELECT thread_id FROM email_threads" + _REOPEN_TERMINAL).fetchall()]
    if not dry_run and reopen_ids:
        cur.execute("""
            UPDATE email_threads
               SET status='pending',
                   last_status_change_at=datetime('now'),
                   updated_at=datetime('now')
        """ + _REOPEN_TERMINAL)
        try:
            import json as _json
            cur.execute(
                "INSERT INTO audit_events (event, meta) VALUES ('thread_reopened', ?)",
                (_json.dumps({"source": "inbox_triage.reconcile_replies (reopen scan)",
                              "count": len(reopen_ids), "thread_ids": reopen_ids[:50]}),))
        except sqlite3.OperationalError:
            pass

    # 1) Mark threads as 'replied' if last_outbound > last_inbound
    #    (ALL non-terminal statuses, not just 'pending')
    rows = cur.execute("""
        SELECT thread_id FROM email_threads
         WHERE status IN ('pending','waiting','draft_pending')
           AND last_outbound_ts IS NOT NULL
           AND last_outbound_ts > COALESCE(last_inbound_ts, '0000-01-01')
    """).fetchall()
    thread_fix = len(rows)
    if not dry_run and rows:
        cur.execute("""
            UPDATE email_threads
               SET status='replied',
                   last_status_change_at=datetime('now'),
                   updated_at=datetime('now')
             WHERE status IN ('pending','waiting','draft_pending')
               AND last_outbound_ts IS NOT NULL
               AND last_outbound_ts > COALESCE(last_inbound_ts, '0000-01-01')
        """)

    # 2) Close action_items where the linked thread shows we-replied-last
    #    (status 'replied' OR 'resolved' — either way, the thread is done from
    #    our side and the open AI is stale).
    ai_rows = cur.execute("""
        SELECT ai.item_id FROM action_items ai
          JOIN email_threads et ON et.thread_id = ai.email_thread_id
         WHERE ai.status='OPEN'
           AND et.status IN ('replied','resolved','resolved_elsewhere','no_action_needed')
           AND et.last_outbound_ts IS NOT NULL
           AND et.last_outbound_ts > COALESCE(et.last_inbound_ts, '0000-01-01')
    """).fetchall()
    ai_fix = len(ai_rows)
    if not dry_run and ai_rows:
        cur.execute("""
            UPDATE action_items
               SET status='DONE',
                   resolution_note='auto-closed: outbound newer than last inbound on linked thread (inbox_triage reconcile-replies)',
                   completed_at=datetime('now'),
                   updated_at=datetime('now'),
                   last_status_change_at=datetime('now')
             WHERE status='OPEN'
               AND email_thread_id IN (
                   SELECT thread_id FROM email_threads
                    WHERE status IN ('replied','resolved','resolved_elsewhere','no_action_needed')
                      AND last_outbound_ts IS NOT NULL
                      AND last_outbound_ts > COALESCE(last_inbound_ts, '0000-01-01')
               )
        """)

    clear_actor(conn)
    if not dry_run:
        conn.commit()
    conn.close()
    return {"threads_marked_replied": thread_fix, "action_items_closed": ai_fix,
            "threads_reopened": len(reopen_ids)}


def status_report() -> dict:
    conn = connect()
    cur = conn.cursor()
    # Per-lane breach counts come FROM v_inbox_breaches (single source of
    # truth) instead of a drifting inline copy of its predicate, and
    # total_breaches is a COUNT(*) -- it used to be len() of a LIMIT-200 row
    # list, so any backlog past 200 made TOTAL disagree with the lane sums.
    lane_counts = cur.execute("""
        SELECT et.lane, COUNT(*) AS total,
               SUM(CASE WHEN et.status='pending' THEN 1 ELSE 0 END) AS pending,
               COALESCE(b.breached, 0) AS breached
          FROM email_threads et
          LEFT JOIN (SELECT lane, COUNT(*) AS breached FROM v_inbox_breaches GROUP BY lane) b
                 ON b.lane = et.lane
         GROUP BY et.lane
    """).fetchall()
    total_breaches = cur.execute("SELECT COUNT(*) FROM v_inbox_breaches").fetchone()[0]
    breach_rows = cur.execute("SELECT * FROM v_inbox_breaches LIMIT 200").fetchall()
    conn.close()
    return {
        "by_lane": [dict(r) for r in lane_counts],
        "total_breaches": total_breaches,
        "breaches": [dict(r) for r in breach_rows],
    }


def daily_payload() -> dict:
    """Return JSON-shaped payload for briefing / kanban consumption."""
    conn = connect()
    cur = conn.cursor()
    lanes: dict[str, list] = {l: [] for l in LANES_ORDERED}
    rows = cur.execute("""
        SELECT et.thread_id, et.lane, et.stakeholder_tier, et.subject,
               et.last_inbound_ts, et.freshness_sla_hours,
               CAST((julianday('now') - julianday(et.last_inbound_ts)) * 24 AS INTEGER) AS age_hours,
               et.person_name_cached, et.last_sender_email, et.context_slug
          FROM email_threads et
         WHERE et.status='pending'
           AND et.last_inbound_ts IS NOT NULL
           AND et.last_inbound_ts >= datetime('now','-60 days')
           AND (et.last_outbound_ts IS NULL OR et.last_inbound_ts > et.last_outbound_ts)
           AND et.lane IS NOT NULL
         ORDER BY et.stakeholder_tier, et.last_inbound_ts
    """).fetchall()
    for r in rows:
        d = dict(r)
        d["sla_breached"] = (d["freshness_sla_hours"] is not None
                             and d["age_hours"] is not None
                             and d["age_hours"] > d["freshness_sla_hours"])
        lanes.setdefault(d["lane"], []).append(d)
    conn.close()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lanes": lanes,
        "counts": {k: len(v) for k, v in lanes.items()},
        "breaches_by_tier": _breach_counts_by_tier(lanes),
    }


def _breach_counts_by_tier(lanes: dict[str, list]) -> dict[int, int]:
    by_tier: dict[int, int] = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    for lane_rows in lanes.values():
        for r in lane_rows:
            if r.get("sla_breached"):
                by_tier[r["stakeholder_tier"]] = by_tier.get(r["stakeholder_tier"], 0) + 1
    return by_tier


def classify_new(window_hours: int = 26, verbose: bool = False) -> dict:
    """Incremental: backfill + classify threads with activity in last N hours.

    Called from brief.py sync. Cheap, idempotent."""
    # Self-heal the freshness cache BEFORE anything reads it: repairs threads
    # whose aggregates were missed by any past sync window, so classification,
    # auto-close, and reconcile_replies below see true state.
    repaired = refresh_thread_cache(days=14, verbose=verbose)
    backfilled = backfill(days=14, verbose=False)
    conn = connect()
    cur = conn.cursor()
    # Auto-close-at-ingest flips status -> entities mirror UPDATE; the blocking
    # write gate aborts NULL-actor writes, so attribute the whole pass.
    set_actor(conn, "inbox_triage:classify-ingest")
    # Re-classify any thread whose lane is null OR whose newest message arrived
    # after our last classification pass (auto_classified_at). Avoids the
    # infinite-reclassify loop that comes from comparing against updated_at.
    rows = cur.execute("""
        SELECT thread_id, subject, last_sender_email, person_id, context_slug
          FROM email_threads
         WHERE lane IS NULL
            OR auto_classified_at IS NULL
            OR (
                COALESCE(last_inbound_ts, '0000-01-01') > COALESCE(auto_classified_at, '0000-01-01')
                AND last_inbound_ts >= datetime('now', ?)
            )
    """, (f"-{window_hours} hours",)).fetchall()
    cls = Classifier(conn)
    meta = _recipient_meta(cur, [r["thread_id"] for r in rows])
    n = 0
    autoclosed = 0
    for r in rows:
        grp, sname, downer, to_us = meta.get(r["thread_id"], (False, None, None, False))
        result = cls.classify_thread(
            r["last_sender_email"], r["subject"], r["person_id"], r["context_slug"],
            group_recipient=grp, last_sender_name=sname,
            delegated_owner=downer, addressed_to_us=to_us,
        )
        cur.execute("""
            UPDATE email_threads
               SET lane=?, tier_base=?, stage_boost=?, stakeholder_tier=?,
                   freshness_sla_hours=?, classification_source=?,
                   auto_classified_at=datetime('now')
             WHERE thread_id=?
        """, (result["lane"], result["tier_base"], result["stage_boost"],
              result["tier"], result["sla_hours"], result["source"],
              r["thread_id"]))
        n += 1

        # Auto-close at ingest. Skip if a canonical action_item already
        # protects this thread (we don't want to silently close something the
        # operator tracked manually).
        should_close, reason = is_auto_close_candidate(
            {"lane": result["lane"],
             "last_sender_email": r["last_sender_email"],
             "subject": r["subject"]},
            last_inbound_body=None,
        )
        if should_close:
            protected = cur.execute(
                "SELECT 1 FROM action_items "
                "WHERE email_thread_id=? AND status IN ('OPEN','WAITING','BLOCKED') LIMIT 1",
                (r["thread_id"],),
            ).fetchone()
            if not protected:
                cur.execute("""
                    UPDATE email_threads
                       SET status='no_action_needed',
                           resolution_note=COALESCE(resolution_note,'')
                               || ' [auto-close at ingest: ' || ? || ']',
                           resolved_at=datetime('now'),
                           last_status_change_at=datetime('now')
                     WHERE thread_id=? AND status='pending'
                """, (reason, r["thread_id"]))
                if cur.rowcount:
                    autoclosed += 1
    # Commit pending UPDATEs before reconcile_replies, which opens its own
    # connection and would otherwise deadlock against our held write txn.
    clear_actor(conn)
    conn.commit()
    # Reconcile false-positive replies
    rec = reconcile_replies(dry_run=False)
    conn.execute("""
        INSERT INTO sync_state (source, last_sync, count)
        VALUES ('inbox_triage', CURRENT_TIMESTAMP, ?)
        ON CONFLICT(source) DO UPDATE SET last_sync=CURRENT_TIMESTAMP, count=excluded.count
    """, (n,))
    conn.commit()
    conn.close()
    if verbose:
        print(f"inbox_triage: cache_repaired={repaired} backfilled={backfilled} classified={n} autoclosed={autoclosed} {rec}")
    return {"cache_repaired": repaired, "backfilled": backfilled, "classified": n, "autoclosed": autoclosed, **rec}


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    p_cls = sub.add_parser("classify")
    p_cls.add_argument("--all", action="store_true")
    p_cls.add_argument("--new", action="store_true")
    p_cls.add_argument("--missing", action="store_true")
    p_cls.add_argument("--recent", action="store_true", help="threads with activity in the last --days")
    p_cls.add_argument("--days", type=int, default=14)
    p_cls.add_argument("--thread")
    p_cls.add_argument("--person", type=int, help="re-classify every thread linked to person_id (use after role change)")
    p_cls.add_argument("--dry-run", action="store_true")
    p_cls.add_argument("--verbose", "-v", action="store_true")

    p_bf = sub.add_parser("backfill")
    p_bf.add_argument("--days", type=int, default=365)
    p_bf.add_argument("--verbose", "-v", action="store_true")

    sub.add_parser("reconcile-replies").add_argument("--dry-run", action="store_true")

    sub.add_parser("status")

    p_br = sub.add_parser("breaches")
    p_br.add_argument("--lane")

    sub.add_parser("daily")
    sub.add_parser("classify-new")

    p_acs = sub.add_parser(
        "auto-close-stale",
        help="Post-hoc auto-close: outbound>inbound -> replied; general >30d idle -> stale",
    )
    p_acs.add_argument("--dry-run", action="store_true")

    p_scan = sub.add_parser(
        "scan-asks",
        help="Scan recent sources for unsurfaced asks (commitment claims; requires the extraction layer)",
    )
    p_scan.add_argument("--since", type=int, default=24, help="hours back")
    p_scan.add_argument(
        "--sources",
        default="commitments",
        help="comma-separated source list (currently: commitments)",
    )
    p_scan.add_argument(
        "--format", choices=["markdown", "json"], default="markdown",
    )
    p_scan.add_argument(
        "--ai-id-column", action="store_true", default=True,
        help="include AI-ID column for dedup awareness (placeholder; needs commitment->AI resolution)",
    )
    p_scan.add_argument("--limit", type=int, default=200)

    args = p.parse_args()

    if args.cmd == "classify":
        if args.thread:
            scope = "thread"
        elif args.person is not None:
            scope = "person"
        elif args.missing:
            scope = "missing"
        elif args.recent:
            scope = "recent"
        else:
            scope = "all"
        n = classify(scope=scope, thread_id=args.thread, person_id=args.person,
                     days=args.days, dry_run=args.dry_run, verbose=args.verbose)
        print(f"classified: {n}{' (dry-run)' if args.dry_run else ''}")

    elif args.cmd == "backfill":
        n = backfill(days=args.days, verbose=args.verbose)
        print(f"backfilled: {n} new email_threads rows")

    elif args.cmd == "reconcile-replies":
        res = reconcile_replies(dry_run=getattr(args, "dry_run", False))
        print(json.dumps(res, indent=2))

    elif args.cmd == "status":
        rep = status_report()
        print("=" * 60)
        print(f"{'Lane':<20}{'Total':>8}{'Pending':>10}{'Breached':>10}")
        print("-" * 60)
        for r in rep["by_lane"]:
            lane = r["lane"] or "(unclassified)"
            print(f"{lane:<20}{r['total']:>8}{r['pending']:>10}{r['breached']:>10}")
        print("-" * 60)
        print(f"TOTAL SLA BREACHES: {rep['total_breaches']}")

    elif args.cmd == "breaches":
        conn = connect()
        sql = "SELECT * FROM v_inbox_breaches"
        params = ()
        if args.lane:
            sql += " WHERE lane=?"
            params = (args.lane,)
        rows = conn.execute(sql, params).fetchall()
        for r in rows:
            print(f"[tier {r['stakeholder_tier']}] [{r['lane']}] {r['age_hours']}h | "
                  f"{r['person_name_cached'] or r['last_sender_email']} | {r['subject']}")
        print(f"\n{len(rows)} breaches")

    elif args.cmd == "daily":
        print(json.dumps(daily_payload(), indent=2, default=str))

    elif args.cmd == "classify-new":
        res = classify_new(verbose=True)
        print(json.dumps(res, indent=2))

    elif args.cmd == "auto-close-stale":
        res = auto_close_stale(dry_run=args.dry_run)
        print(json.dumps(res, indent=2))

    elif args.cmd == "scan-asks":
        # Surfaces approved commitment claims that aren't yet tracked. The
        # commitment scanner is part of the claim-extraction layer, which is
        # NOT included in this starter kit; without it this subcommand
        # degrades to an empty report. If the operator later adds a
        # compatible commitment_consumer.py next to this file (exposing
        # scan_candidates / synthesize_context / build_source_url), it is
        # picked up automatically.
        cc_path = Path(__file__).resolve().parent / "commitment_consumer.py"
        if not cc_path.is_file():
            note = ("scan-asks: commitment scanning needs the extraction layer, "
                    "which is not included in this starter kit; nothing to scan")
            print(note, file=sys.stderr)
            if args.format == "json":
                print(json.dumps([]))
            else:
                print(f"# scan-asks (0 candidates, last {args.since}h)\n")
                print("_Extraction layer not installed; no commitment candidates._")
            return
        import importlib.util as _iu
        _spec = _iu.spec_from_file_location("commitment_consumer", str(cc_path))
        _cc = _iu.module_from_spec(_spec)
        _spec.loader.exec_module(_cc)
        conn = connect()
        cands = _cc.scan_candidates(conn, since_hours=args.since, limit=args.limit)
        for c in cands:
            c["_context"] = _cc.synthesize_context(conn, c)
            c["_source_url"] = _cc.build_source_url(c)
        # Sources beyond commitments are placeholders for now.
        wanted_sources = set(s.strip() for s in args.sources.split(","))
        if "commitments" not in wanted_sources:
            cands = []
        if args.format == "json":
            print(json.dumps(cands, indent=2, default=str))
        else:
            print(f"# scan-asks ({len(cands)} candidates, last {args.since}h)\n")
            if not cands:
                print("_No new commitment candidates._")
            else:
                print("| # | AI-ID | Link | Context |")
                print("|---|---|---|---|")
                for i, c in enumerate(cands, 1):
                    link_url = c.get("_source_url", "-")
                    link_kind = "gmail" if link_url and "mail.google.com" in link_url else "claim"
                    link_md = f"[{link_kind}]({link_url})" if link_url else "-"
                    print(f"| {i} | claim:{c['claim_id']} | {link_md} | "
                          f"{(c.get('_context') or c.get('claim_text') or '')[:100]} |")
        conn.close()


if __name__ == "__main__":
    main()
