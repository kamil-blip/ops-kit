"""UserPromptSubmit hook: injects relevant context before Claude processes each prompt.

Consolidates keyword memory matching + learning surfacing + template step matching.
Queries the ops database instead of scanning flat files.

Three context injections for substantial prompts:
1. Memory file matching (optional memory_files index table; skipped if absent)
2. Action item matching (queries action_items table)
3. Template step matching (the thinking loop)

Always checks deferred actions count.

Workflow routes (THING 5) run for ALL non-trivial prompts. The
workflow_routes table ships EMPTY in this starter kit: with 0 rows nothing
fires, and every route you add later starts surfacing automatically.

ROUTING_V2 (env): when 1, applies per-THING caps (1 each), global cap N=4, domain
inference, and domain-mismatch filter. Suppressions logged to audit_events. v1
remains the default until enough telemetry validates the v2 noise floor.
"""
import io
import json
import os
import re
import sys

# Windows console (cp1252) chokes on Unicode chars in surfaced learnings/routes.
# Force UTF-8 with replacement so the hook never breaks on encoding alone.
# reconfigure IN PLACE (never swap the stream object -- replacing sys.stdout
# at import time discards the importer's unflushed output and breaks streams
# without .buffer; fatal for a stdio MCP host).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError, io.UnsupportedOperation):
    pass

_hooks_dir = os.path.dirname(os.path.abspath(__file__))
if not sys.path or sys.path[0] != _hooks_dir:
    # Front-insert so hooks/config.py wins over core/config.py when this
    # module is imported in-process (script execution already has it first).
    sys.path.insert(0, _hooks_dir)

from config import get_conn, set_session_id
from health_monitor import timed

ROUTING_V2 = os.environ.get("ROUTING_V2", "0") == "1"

STOP_WORDS = {
    "the", "and", "for", "that", "this", "with", "from", "what", "how", "can",
    "please", "lets", "about", "need", "want", "know", "check", "also", "like",
    "just", "have", "been", "will", "should", "could", "would", "into", "some",
    "when", "where", "which", "there", "their", "they", "them", "then", "than",
    "other", "more", "most", "make", "made", "does", "doing", "done", "work",
    "working", "here", "very", "each", "only", "these", "those", "after", "before",
}

SIMPLE_ANSWERS = {"yes", "no", "ok", "done", "next", "continue", "send", "copy", "sure", "yep", "nope", "yeah"}

# v2: domain vocabulary for prompt classification. Cheap regex/keyword match.
# Ships EMPTY: fill each set with the words that mark a prompt as belonging to
# that sphere of your life. Default 'general' on uncertainty so unknown prompts
# surface everything (preserves v1 behaviour); with empty sets, everything is
# 'general' and the domain filter is inert.
DOMAIN_VOCAB = {
    "work": set(),
    # FICTIONAL example:
    # "work": {"invoice", "client", "sprint", "deploy", "standup", "acme"},
    "personal": set(),
    # "personal": {"flight", "rent", "doctor", "dentist", "taxes", "passport"},
    "family": set(),
    # "family": {"mum", "dad", "sister", "brother"},
    "public": set(),
    # "public": {"blog", "announcement", "website", "launch", "press", "newsletter"},
}


def extract_words(text, min_len=4):
    """Extract meaningful words from text, filtering stop words."""
    words = set(re.findall(r'[a-zA-Z]{%d,}' % min_len, text.lower()))
    return words - STOP_WORDS


def word_overlap(words_a, words_b):
    """Count overlapping words between two sets."""
    return len(words_a & words_b)


ROUTE_FIRE_THRESHOLD = 2


def score_route(triggers, prompt_lower, prompt_words_3):
    """THE workflow-route scoring predicate. Any offline lint/gauge tooling must
    import this rather than fork it; a forked copy once de-synced the miss gauge,
    so any scoring change happens here and nowhere else."""
    triggers = triggers or ""
    if "|" in triggers:
        # Phrase mode: pipe-separated segments are matched as whole substrings
        # of the prompt, never exploded into a word bag. Two generic shared
        # words can no longer co-fire a route (a known miss-inflation mechanism).
        segs = [s.strip().lower() for s in triggers.split("|") if s.strip()]
        hits = sum(1 for s in segs if s in prompt_lower)
        return 2 * hits  # one phrase hit = fire; scale to legacy scoring
    trigger_words = extract_words(triggers, min_len=3)
    overlap = word_overlap(prompt_words_3, trigger_words)
    # Also check for exact substring matches (e.g. "email" in prompt)
    exact_hits = len(set(triggers.lower().split()) & set(prompt_lower.split()))
    return overlap + exact_hits


def select_fired_routes(route_matches):
    """Selection rule (shared with any offline lint tooling): sort by score then
    priority, scan the top 3, dedup by required_action, surface at most 2."""
    ordered = sorted(route_matches, key=lambda x: (-x[1], x[0]["priority"]))
    chosen, seen_actions = [], set()
    for route, score in ordered[:3]:
        action = route["required_action"]
        if action in seen_actions:
            continue
        seen_actions.add(action)
        chosen.append((route, score))
        if len(chosen) >= 2:
            break
    return chosen


def infer_domain(prompt_words):
    """v2: cheap regex/keyword classifier. Returns 'work'|'personal'|'family'|'public'|'general'.

    Default 'general' when the top score is < 2 (preserves v1 surface behaviour).
    """
    scores = {d: word_overlap(prompt_words, set(v)) for d, v in DOMAIN_VOCAB.items()}
    if not scores:
        return "general"
    top = max(scores, key=scores.get)
    if scores[top] >= 2:
        return top
    return "general"


def _domain_drops(prompt_domain, artifact_domain):
    """v2: True iff this artifact should be suppressed for domain mismatch.

    Rule: drop only when both prompt and artifact have a non-general domain AND
    they disagree. General artifacts always pass (safety routes default general).
    """
    if prompt_domain == "general" or not artifact_domain or artifact_domain == "general":
        return False
    return artifact_domain != prompt_domain


def _log_suppression(conn, session_id, kind, prompt_domain, artifact_domain, ref):
    """v2: log a single suppression event so later tuning has data."""
    try:
        meta = json.dumps({
            "kind": kind,
            "prompt_domain": prompt_domain,
            "artifact_domain": artifact_domain,
            "ref": ref,
        }, separators=(",", ":"))
        conn.execute(
            "INSERT INTO audit_events (session_id, event, tool, meta, ts) "
            "VALUES (?, 'surface_suppressed', 'context_injector', ?, datetime('now'))",
            (session_id or "", meta),
        )
    except Exception:
        pass


def _table_exists(conn, name):
    """Feature-detect an optional table (e.g. memory_files is not part of the
    starter schema; create it yourself if you want an indexed memory layer)."""
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except Exception:
        return False


@timed("context_injector")
def run(event):
    # Prime session_id from Claude Code payload so this UserPromptSubmit shares
    # an ID with the Stop hook that will process it. Must happen before any DB
    # write so learning_reviews.session_id matches what Stop queries.
    session_id = event.get("session_id", "") or event.get("sessionId", "")
    set_session_id(session_id)

    prompt = event.get("prompt", "")
    if not prompt:
        return

    prompt_lower = prompt.lower().strip()

    # Skip commands and simple answers
    if prompt_lower.startswith("/"):
        return
    if prompt_lower in SIMPLE_ANSWERS:
        return

    context_parts = []
    # Scored buckets for v2 global cap (kind, score, line)
    scored_parts = []
    prompt_words_4 = extract_words(prompt, min_len=4)
    prompt_words_3 = extract_words(prompt, min_len=3)
    prompt_domain = infer_domain(prompt_words_3) if ROUTING_V2 else "general"

    # THINGS 1-4 need substantive prompts (keyword density); THING 5 runs for all non-trivial prompts
    # so short canonical commands ("brief", "wrap up") still fire workflow routes.
    is_long_enough_for_heavy = len(prompt) >= 10
    is_substantial = is_long_enough_for_heavy and len(prompt) > 40 and len(prompt_words_4) >= 2

    try:
        conn = get_conn()
    except Exception:
        return

    # v1 cap is 3 memory_files; v2 cap is 1.
    mem_cap = 1 if ROUTING_V2 else 3

    # Track action_item for global-cap preservation in v2.
    action_item_line = None

    try:
        if is_substantial:
            # THING 1: Memory file matching (only if a memory_files index exists)
            try:
                if _table_exists(conn, "memory_files"):
                    rows = conn.execute(
                        "SELECT filename, description, COALESCE(domain,'general') as domain FROM memory_files "
                        "WHERE filename NOT IN ('MEMORY.md') AND COALESCE(type,'') != 'archived'"
                    ).fetchall()

                    matches = []
                    for row in rows:
                        desc = row["description"] or ""
                        fname = row["filename"] or ""
                        desc_words = extract_words(desc, min_len=4) | extract_words(fname.replace("-", " ").replace(".md", ""), min_len=4)
                        overlap = word_overlap(prompt_words_4, desc_words)
                        if overlap >= 3:
                            if ROUTING_V2 and _domain_drops(prompt_domain, row["domain"]):
                                _log_suppression(conn, session_id, "memory_file", prompt_domain, row["domain"], fname)
                                continue
                            matches.append((fname, overlap))

                    if matches:
                        matches.sort(key=lambda x: -x[1])
                        top = matches[:mem_cap]
                        line = "Relevant memory files: " + ", ".join(f[0] for f in top)
                        if ROUTING_V2:
                            scored_parts.append(("memory", top[0][1], line))
                        else:
                            context_parts.append(line)
            except Exception:
                pass

            # THING 2: Action item matching (always preserved in v2 global cap)
            try:
                rows = conn.execute(
                    "SELECT status, priority, description, COALESCE(domain,'general') as domain "
                    "FROM action_items "
                    "WHERE status IN ('OPEN', 'WAITING', 'BLOCKED') "
                    "ORDER BY priority, inserted_at"
                ).fetchall()

                for row in rows:
                    desc = row["description"] or ""
                    item_words = extract_words(desc, min_len=3)
                    overlap = word_overlap(prompt_words_3, item_words)
                    if overlap >= 2:
                        if ROUTING_V2 and _domain_drops(prompt_domain, row["domain"]):
                            _log_suppression(conn, session_id, "action_item", prompt_domain, row["domain"], desc[:80])
                            continue
                        status = row["status"]
                        priority = row["priority"] or "P2"
                        action_item_line = f"Related action item: [{status}] [{priority}] {desc}"
                        context_parts.append(action_item_line)
                        break  # Just one match to keep context small
            except Exception:
                pass

            # THING 3: Template step matching
            try:
                if _table_exists(conn, "template_tasks"):
                    rows = conn.execute(
                        "SELECT step_number, task_name, description, definition_of_done, tools_needed, "
                        "       COALESCE(domain,'general') as domain "
                        "FROM template_tasks"
                    ).fetchall()

                    tt_matches = []
                    for row in rows:
                        task_name = row["task_name"] or ""
                        desc = row["description"] or ""
                        combined_words = extract_words(task_name, min_len=4) | extract_words(desc, min_len=4)
                        overlap = word_overlap(prompt_words_4, combined_words)
                        if overlap >= 3:
                            if ROUTING_V2 and _domain_drops(prompt_domain, row["domain"]):
                                _log_suppression(conn, session_id, "template_task", prompt_domain, row["domain"], task_name)
                                continue
                            tt_matches.append((overlap, row))
                    if tt_matches:
                        tt_matches.sort(key=lambda x: (-x[0], x[1]["step_number"] or 0))
                        best = tt_matches[0][1]
                        step = best["step_number"]
                        dod = best["definition_of_done"] or "N/A"
                        line = f"Template step {step}: {best['task_name']} | Done when: {dod}"
                        if ROUTING_V2:
                            scored_parts.append(("template", tt_matches[0][0], line))
                        else:
                            context_parts.append(line)
            except Exception:
                pass

            # THING 4: Learning surfacing (v2 explicitly caps at 1)
            try:
                from learning_loop import surface_learnings
                learning_matches = surface_learnings(prompt_words_4, conn)
                if learning_matches:
                    line = learning_matches[0]
                    if ROUTING_V2:
                        scored_parts.append(("learning", 1, line))
                    else:
                        context_parts.extend(learning_matches[:1])
            except Exception:
                pass

        # THING 5: Workflow route matching (runs for ALL non-trivial prompts)
        # Surfaces required skills/checks before the model starts working.
        # Lower threshold than "substantial" -- even a short "send DM to Jose"
        # must trigger guards. With an empty workflow_routes table this loop
        # simply matches nothing.
        try:
            routes = conn.execute(
                "SELECT id, trigger_patterns, required_action, reason, priority, "
                "       COALESCE(domain,'general') as domain "
                "FROM workflow_routes WHERE active = 1 "
                "ORDER BY priority"
            ).fetchall()

            route_matches = []
            for route in routes:
                score = score_route(route["trigger_patterns"], prompt_lower, prompt_words_3)
                if score >= ROUTE_FIRE_THRESHOLD:
                    if ROUTING_V2 and _domain_drops(prompt_domain, route["domain"]):
                        _log_suppression(conn, session_id, "workflow_route", prompt_domain, route["domain"], route["required_action"][:80])
                        continue
                    route_matches.append((route, score))

            for route, score in select_fired_routes(route_matches):
                action = route["required_action"]
                line = f"Required workflow: {action}"
                if ROUTING_V2:
                    # Routes always get scored; safety routes (domain=general) score
                    # higher than generic matches via priority bias.
                    priority_bias = (1000 - (route["priority"] or 50)) / 1000.0
                    scored_parts.append(("route", score + priority_bias, line))
                else:
                    context_parts.append(line)
                # exact-fire log so offline tooling can audit route coverage
                try:
                    conn.execute(
                        "INSERT INTO bus_events (session_id, event_type, summary, details_json, ts) "
                        "VALUES (?, 'route_fired', ?, ?, datetime('now'))",
                        (session_id, action[:120],
                         json.dumps({"route_id": route["id"], "score": score,
                                     "domain": route["domain"]})),
                    )
                    conn.commit()
                except Exception:
                    pass
        except Exception:
            pass

        # ALWAYS: Deferred actions count
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM deferred_actions WHERE status = 'pending'"
            ).fetchone()[0]
            if count > 0:
                context_parts.append(f"Note: {count} deferred actions pending")
        except Exception:
            pass

        # v2 global cap: keep top N=4 by score plus action_item + deferred-actions count
        # (action_item is already in context_parts; routes/memory/template/learning are in scored_parts).
        if ROUTING_V2 and scored_parts:
            scored_parts.sort(key=lambda x: -x[1])
            # Reserve 1 slot for action_item (if present), so global cap = 4 lines including it.
            slot_budget = 4 - (1 if action_item_line else 0)
            for kind, score, line in scored_parts[:slot_budget]:
                context_parts.append(line)

        if ROUTING_V2:
            try:
                conn.commit()  # Flush suppression log writes
            except Exception:
                pass

    finally:
        conn.close()

    if context_parts:
        print("\n".join(context_parts))


if __name__ == "__main__":
    try:
        data = json.load(sys.stdin)
        run(data)
    except Exception as e:
        print(f"HOOK ERROR (context_injector.py): {e}", file=sys.stderr)

    sys.exit(0)
