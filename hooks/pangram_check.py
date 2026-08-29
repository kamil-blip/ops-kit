"""Pangram AI-text detector gate for outbound drafts (optional, paid).

Why this exists: a phrase blacklist (quality_gate.py) and a structural linter
(slop_stats.py) both passed a draft that a recipient then ran through Pangram
and got "100% AI". If the people you write to might run a detector, run it
first yourself. This module is a thin client for Pangram's API; it is not
wired into any hook chain by default because every call costs money.

API (docs.pangram.com/api-reference/ai-detection):
  POST https://text.external-api.pangram.com/task   {"text": ..., "model": "pangram-4"}
      -> {"task_id": ...}
  GET  https://text.external-api.pangram.com/task/{task_id}
      -> {"stage": "STAGE_SUCCESS"|"STAGE_FAILED"|..., "prediction_short": "AI"|"Human"|"Mixed",
          "fraction_ai": 0-1, "fraction_ai_assisted": 0-1, "fraction_human": 0-1,
          "windows": [{"label": "AI-Generated"|"AI-Assisted"|"Human Written", "confidence": ...,
                       "start_index", "end_index", "word_count", ...}], ...}
  Header x-api-key. Pay as you go, per 100 words on pangram-4. 50-word minimum.

Key lookup (first hit wins):
  1. env PANGRAM_API_KEY
  2. OS keyring, service "ops-kit", username "PANGRAM_API_KEY"
     (python -c "import keyring; keyring.set_password('ops-kit','PANGRAM_API_KEY','<KEY>')")

Behaviour:
  * no key           -> silent skip (fail-open)
  * < 50 words       -> skip with a note
  * API error        -> warn, allow (fail-open)
  * prediction AI or fraction_ai >= BLOCK_AT -> "would block" verdict
  * otherwise        -> score line plus any AI-labelled windows

Use it sparingly: score a draft once, fix the flagged window, re-score once.
Copy a passing fix across near-identical drafts instead of re-scoring each.

CLI:  python pangram_check.py file.txt [...]      score files (same key lookup)
Library: score(text) -> dict; verdict(result) -> (would_block: bool, line: str)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = "https://text.external-api.pangram.com"
MODEL = "pangram-4"
MIN_WORDS = 50
BLOCK_AT = 0.50        # fraction_ai at or above this counts as a block
POLL_EVERY = 1.0
POLL_MAX_S = 60
KEYRING_SERVICE = "ops-kit"
KEYRING_USER = "PANGRAM_API_KEY"


def get_key() -> str | None:
    k = os.environ.get("PANGRAM_API_KEY")
    if k:
        return k.strip()
    try:
        import keyring  # noqa: PLC0415
        k = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        return k.strip() if k else None
    except Exception:  # noqa: BLE001
        return None


def _req(method: str, path: str, key: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"x-api-key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def strip_html(text: str) -> str:
    """Drop HTML tags and quoted reply history so only the new body is scored."""
    if "<" in text and ">" in text:
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        import html as _h  # noqa: PLC0415
        text = _h.unescape(text)
    m = re.search(r"\n\s*On .{5,120} wrote:\s*\n", text)
    if m:
        text = text[: m.start()]
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))
    return re.sub(r"[ \t]+", " ", text).strip()


def score(text: str, key: str | None = None) -> dict:
    """Return {"ok": bool, "skipped": reason|None, "prediction": str, "fraction_ai": float,
    "windows": [...], "words": n, "error": str|None, "is_humanized": bool|None}."""
    key = key or get_key()
    clean = strip_html(text or "")
    words = len(re.findall(r"[A-Za-z][A-Za-z'’-]*", clean))
    out = {"ok": False, "skipped": None, "prediction": None, "fraction_ai": None,
           "windows": [], "words": words, "error": None, "is_humanized": None}
    if not key:
        out["skipped"] = "no key"; return out
    if words < MIN_WORDS:
        out["skipped"] = f"{words} words (<{MIN_WORDS} minimum)"; return out
    try:
        t = _req("POST", "/task", key, {"text": clean, "model": MODEL, "public_dashboard_link": True})
        tid = t.get("task_id")
        if not tid:
            out["error"] = f"no task_id in {str(t)[:200]}"; return out
        deadline = time.time() + POLL_MAX_S
        while time.time() < deadline:
            r = _req("GET", f"/task/{tid}", key)
            stage = r.get("stage", "")
            if stage == "STAGE_SUCCESS":
                out.update(ok=True, prediction=r.get("prediction_short"),
                           fraction_ai=r.get("fraction_ai"),
                           is_humanized=any(w.get("is_humanized") for w in r.get("windows", [])),
                           dashboard=r.get("dashboard_link"),
                           windows=[w for w in r.get("windows", []) if w.get("label") != "Human Written"])
                return out
            if stage == "STAGE_FAILED":
                out["error"] = f"task failed: {str(r)[:200]}"; return out
            time.sleep(POLL_EVERY)
        out["error"] = "timeout waiting for Pangram"; return out
    except urllib.error.HTTPError as e:
        out["error"] = f"HTTP {e.code}: {e.read().decode(errors='ignore')[:200]}"; return out
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"; return out


def verdict(res: dict) -> tuple[bool, str]:
    """(would_block?, human-readable line). Scores are reported as '% AI'."""
    if res.get("skipped"):
        return False, f"PANGRAM: skipped ({res['skipped']})"
    if res.get("error"):
        return False, f"PANGRAM: error, allowing ({res['error']})"
    fa = res.get("fraction_ai") or 0.0
    line = f"PANGRAM: {res.get('prediction')} ({fa:.0%} AI, {res['words']} words" + \
           (", humanized" if res.get("is_humanized") else "") + ")"
    if res.get("dashboard"):
        line += f"\n  view: {res['dashboard']}"
    wins = res.get("windows") or []
    if wins:
        line += "\n  flagged windows: " + " | ".join(
            f"{w.get('label')} conf={w.get('confidence')} words={w.get('word_count')}" for w in wins[:6])
    block = (res.get("prediction") == "AI") or fa >= BLOCK_AT
    return block, line


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    for p in sys.argv[1:]:
        with open(p, encoding="utf-8") as fh:
            r = score(fh.read())
        b, line = verdict(r)
        print(f"{p.replace(chr(92), '/').split('/')[-1]}: {line}{'  -> WOULD BLOCK' if b else ''}")
