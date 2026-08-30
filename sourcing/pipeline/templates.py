"""Outreach templates: a registry, {{placeholder}} rendering, and a lint.

Templates live as Markdown files in sourcing/pipeline/templates/ (first line `subject: ...`,
blank line, then the body) and are loaded into the `templates` table so the rest
of the pipeline can render them by scenario id.

Usage:
    python sourcing/pipeline/templates.py load                     load the files into the database
    python sourcing/pipeline/templates.py list
    python sourcing/pipeline/templates.py render judge-invite --var first_name=Ada --var search_name="..."
    python sourcing/pipeline/templates.py lint path/to/draft.txt   lint any text file

The lint fails on: an unresolved {{placeholder}}, a doubled word ("the The"),
a double space, an em dash in any form, and any phrase in slop_rules.SLOP_BANNED.
The rule behind the phrase list: a reader who suspects a message was machine-written
stops reading it as a message from you.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _db  # noqa: E402
from slop_rules import EM_DASH_PATTERNS, SLOP_BANNED  # noqa: E402

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
DOUBLED = re.compile(r"\b(\w+)\s+\1\b", re.IGNORECASE)


def lint(text: str) -> list[str]:
    fails: list[str] = []
    for m in PLACEHOLDER.finditer(text):
        fails.append(f"unresolved placeholder {{{{{m.group(1)}}}}}")
    for m in DOUBLED.finditer(text):
        if m.group(1).lower() not in {"that", "had", "very"}:  # "that that" and "had had" are real English
            fails.append(f'doubled word "{m.group(0)}"')
    if "  " in text.replace("\n", ""):
        fails.append("double space")
    for pat in EM_DASH_PATTERNS:
        if pat in text:
            fails.append(f"em dash form {pat.strip()!r}")
    low = text.lower()
    for phrase in SLOP_BANNED:
        if phrase.lower() in low:
            fails.append(f"banned phrase {phrase!r}")
    return fails


def render_text(text: str, values: dict) -> str:
    def sub(m):
        v = values.get(m.group(1))
        return str(v) if v is not None else m.group(0)  # leave unresolved so lint catches it
    return PLACEHOLDER.sub(sub, text)


def load(conn) -> int:
    n = 0
    for f in sorted(TEMPLATE_DIR.glob("*.md")):
        raw = f.read_text(encoding="utf-8")
        head, _, body = raw.partition("\n\n")
        subject = head.split(":", 1)[1].strip() if head.lower().startswith("subject:") else ""
        conn.execute("INSERT INTO templates (id, subject, body, updated_at) VALUES (?,?,?,datetime('now')) "
                     "ON CONFLICT(id) DO UPDATE SET subject=excluded.subject, body=excluded.body, updated_at=excluded.updated_at",
                     (f.stem, subject, body.strip() + "\n"))
        n += 1
    conn.commit()
    return n


def render(conn, template_id: str, values: dict) -> dict:
    row = conn.execute("SELECT subject, body FROM templates WHERE id=?", (template_id,)).fetchone()
    if row is None:
        raise SystemExit(f"unknown template: {template_id} (run `load` first)")
    subject = render_text(row["subject"], values)
    body = render_text(row["body"], values)
    return {"id": template_id, "subject": subject, "body": body, "lint": lint(subject) + lint(body)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Outreach template registry, renderer and lint")
    ap.add_argument("--db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("load", help="load sourcing/pipeline/templates/*.md into the database")
    sub.add_parser("list", help="list loaded templates")
    p = sub.add_parser("render", help="render one template"); p.add_argument("template_id")
    p.add_argument("--var", action="append", default=[], help="name=value, repeatable")
    p = sub.add_parser("lint", help="lint a text file"); p.add_argument("path")
    a = ap.parse_args(argv)
    if a.cmd == "lint":
        fails = lint(Path(a.path).read_text(encoding="utf-8"))
        print("\n".join(fails) if fails else "clean")
        return 1 if fails else 0
    conn = _db.connect(a.db)
    if a.cmd == "load":
        print(f"loaded {load(conn)} template(s)")
    elif a.cmd == "list":
        for r in conn.execute("SELECT id, subject, updated_at FROM templates ORDER BY id"):
            print(f"{r['id']:24} {r['subject']}")
    elif a.cmd == "render":
        values = dict(v.split("=", 1) for v in a.var)
        out = render(conn, a.template_id, values)
        print(f"Subject: {out['subject']}\n\n{out['body']}")
        if out["lint"]:
            print("LINT: " + "; ".join(out["lint"]), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
