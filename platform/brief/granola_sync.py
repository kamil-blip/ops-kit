"""
Granola transcript sync.

Pulls all Granola meetings via the Granola REST API (api.granola.ai) and stores
each as a reference_docs row with:
  slug          = transcript-YYYY-MM-DD-{slugified-title}
  category      = transcript
  doc_type      = meeting_transcript
  source_file   = granola
  url           = granola:{meeting_id}   <-- authoritative dedup key
  tags          = granola,meeting
  content       = formatted markdown (header + participants + summary + notes + transcript)

Dedup key is the Granola UUID in `url`, not the slug. Titles in Granola can be
edited after the meeting, so slug-based dedup creates duplicates.

Prerequisites (see INSTALL.md): your own Granola account with the desktop app
installed and signed in. The token store lives in the app's data directory
(%APPDATA%\\Granola on Windows; override with the GRANOLA_DIR env var).

OAuth: reads the Granola desktop app's token store. Newer Granola builds encrypt
this store (stored-accounts.json.enc) via Chromium-style os_crypt -- DPAPI-wrapped
master key in "Local State" decrypts storage.dek, which decrypts the .enc files.
We decrypt that chain to read the current token; the legacy plaintext
stored-accounts.json is a fallback for older installs. If the access_token is
near expiry we refresh via WorkOS and write the rotated tokens back (re-encrypting
the .enc store) so the desktop app stays in sync. The DPAPI decryption chain is
Windows-specific.

Usage:
    python granola_sync.py list                  # List Granola docs vs DB (diff)
    python granola_sync.py sync                  # Pull missing transcripts into DB
    python granola_sync.py sync --since 7        # Only docs created in last 7 days
    python granola_sync.py backfill              # Stamp granola:<uuid> into existing rows by slug match
    python granola_sync.py token                 # Print current/refreshed access token (debug)

Importable by brief.py for automatic ingest on every `brief.py sync`.
"""

from __future__ import annotations

import paths
import config

import argparse
import base64
import gzip
import json
import os
import pathlib
import re
import sqlite3
import _db  # shared connector (busy_timeout + FK ON)
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")

DB = pathlib.Path(str(paths.DB_PATH))
# Granola desktop app data dir. No hardcoded username: resolved from the
# GRANOLA_DIR env var, else %APPDATA%\Granola (the app's own install location).
GRANOLA_DIR = pathlib.Path(
    os.environ.get("GRANOLA_DIR")
    or os.path.expandvars(r"%APPDATA%\Granola")
)
STORED_ACCOUNTS = GRANOLA_DIR / "stored-accounts.json"
STORED_ACCOUNTS_ENC = GRANOLA_DIR / "stored-accounts.json.enc"
LOCAL_STATE = GRANOLA_DIR / "Local State"
STORAGE_DEK = GRANOLA_DIR / "storage.dek"
# Granola's own (product-wide) WorkOS OAuth client id -- the same for every
# install, not an operator secret.
WORKOS_CLIENT_ID = "client_01JZJ0XBDAT8PHJWQY09Y0VD61"
WORKOS_REFRESH_URL = "https://auth.granola.ai/user_management/authenticate"
GRANOLA_API = "https://api.granola.ai"
UA = "Granola/6.2.0"

# Label for the microphone track in rendered transcripts. Fill in config.toml:
#   [operator] display_name = "Jane"   (FICTIONAL example)
OPERATOR_DISPLAY_NAME = (
    str(config.get("operator.display_name") or "").strip()
    or str(config.get("operator_name") or "").strip()
    or "Operator"
)

# ── Token handling ──────────────────────────────────────────────────────────


def _b64d_json(s: str) -> dict:
    s += "=" * (-len(s) % 4)
    return json.loads(base64.urlsafe_b64decode(s))


# ── Encrypted store (Granola ≥ the build that ships stored-accounts.json.enc) ──
# Newer Granola encrypts its account/token store with Chromium-style os_crypt:
#   Local State.os_crypt.encrypted_key  = b"DPAPI" + DPAPI-protected AES master key
#   storage.dek                          = b"v10" + nonce(12) + AES-256-GCM(master, base64(DEK))
#   *.enc files                          = nonce(12) + AES-256-GCM(DEK, plaintext)
# We decrypt the chain to read the *current* refresh/access token. The old
# plaintext stored-accounts.json is kept only as a fallback for older installs.
_dek_cache: bytes | None = None


def _dpapi_unprotect(blob: bytes) -> bytes:
    """Windows DPAPI CryptUnprotectData (current-user scope), via ctypes."""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(blob, len(blob))
    inb = DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    outb = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(outb)
    ):
        raise OSError("DPAPI CryptUnprotectData failed")
    try:
        return ctypes.string_at(outb.pbData, outb.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(outb.pbData)


def _get_dek() -> bytes:
    """Return the 32-byte AES key Granola uses for its *.enc store (cached)."""
    global _dek_cache
    if _dek_cache is not None:
        return _dek_cache
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    ls = json.loads(LOCAL_STATE.read_text(encoding="utf-8"))
    enc_key = base64.b64decode(ls["os_crypt"]["encrypted_key"])
    if enc_key[:5] != b"DPAPI":
        raise RuntimeError("Unexpected os_crypt.encrypted_key format (no DPAPI prefix)")
    master = _dpapi_unprotect(enc_key[5:])
    dek_blob = STORAGE_DEK.read_bytes()
    if dek_blob[:3] != b"v10":
        raise RuntimeError("Unexpected storage.dek format (no v10 prefix)")
    dek_str = AESGCM(master).decrypt(dek_blob[3:15], dek_blob[15:], None)
    dek = base64.b64decode(dek_str)  # DEK is stored base64-wrapped
    _dek_cache = dek
    return dek


def _enc_decrypt(data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(_get_dek()).decrypt(data[:12], data[12:], None)


def _enc_encrypt(plaintext: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    return nonce + AESGCM(_get_dek()).encrypt(nonce, plaintext, None)


def _read_stored() -> tuple[dict, dict, int, str]:
    """Return (raw accounts dict, first-account tokens, index, store_kind).

    store_kind is 'enc' (current encrypted store) or 'plain' (legacy fallback).
    Prefers the encrypted store; falls back to plaintext if it's absent or the
    decrypt chain fails (e.g. cryptography missing, non-Windows, older install).
    """
    if STORED_ACCOUNTS_ENC.exists():
        try:
            raw = json.loads(_enc_decrypt(STORED_ACCOUNTS_ENC.read_bytes()))
            accounts = json.loads(raw["accounts"])
            if accounts:
                toks = json.loads(accounts[0]["tokens"])
                return raw, toks, 0, "enc"
        except Exception as e:
            sys.stderr.write(
                f"[granola] encrypted store read failed ({e}); falling back to plaintext\n"
            )
    raw = json.loads(STORED_ACCOUNTS.read_text(encoding="utf-8"))
    accounts = json.loads(raw["accounts"])
    if not accounts:
        raise RuntimeError("No Granola accounts found in stored-accounts.json")
    toks = json.loads(accounts[0]["tokens"])
    return raw, toks, 0, "plain"


def _write_stored(raw: dict, toks: dict, idx: int, store_kind: str = "plain") -> None:
    accounts = json.loads(raw["accounts"])
    accounts[idx]["tokens"] = json.dumps(toks)
    raw["accounts"] = json.dumps(accounts)
    payload = json.dumps(raw)
    if store_kind == "enc":
        # Re-encrypt with the same DEK and atomically replace the .enc store so
        # the desktop app picks up the rotated refresh token on its next read.
        blob = _enc_encrypt(payload.encode())
        tmp = STORED_ACCOUNTS_ENC.with_name(STORED_ACCOUNTS_ENC.name + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, STORED_ACCOUNTS_ENC)
    else:
        STORED_ACCOUNTS.write_text(payload, encoding="utf-8")


def _refresh(refresh_token: str) -> dict:
    body = {
        "grant_type": "refresh_token",
        "client_id": WORKOS_CLIENT_ID,
        "refresh_token": refresh_token,
    }
    req = urllib.request.Request(
        WORKOS_REFRESH_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        r = urllib.request.urlopen(req, timeout=20)
        return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"Granola refresh failed ({e.code}): {e.read().decode('utf-8', 'replace')[:200]}"
        )


def get_access_token() -> str:
    """Return a valid Granola access token, refreshing if needed.

    Normal case (desktop app running): the encrypted store already holds a
    fresh access token, so we use it directly with no refresh and no write --
    zero risk of clobbering the app's session. We only refresh (and write the
    rotated token back) when the stored token is within 5 min of expiry, which
    in practice means the app has been closed for a while.
    """
    raw, toks, idx, store_kind = _read_stored()
    tok = toks["access_token"]
    try:
        payload = _b64d_json(tok.split(".")[1])
        exp = int(payload.get("exp", 0))
    except Exception:
        exp = 0
    # Refresh if <5min remaining
    if exp - time.time() < 300:
        fresh = _refresh(toks["refresh_token"])
        toks = {
            **toks,
            "access_token": fresh["access_token"],
            "refresh_token": fresh.get("refresh_token", toks["refresh_token"]),
            "obtained_at": int(time.time() * 1000),
        }
        try:
            _write_stored(raw, toks, idx, store_kind)
        except Exception as e:
            sys.stderr.write(
                f"[granola] token write-back failed ({e}); using refreshed token in-memory\n"
            )
        tok = toks["access_token"]
    return tok


# ── HTTP ────────────────────────────────────────────────────────────────────


def _api(path: str, payload: dict, tok: str | None = None) -> Any:
    if tok is None:
        tok = get_access_token()
    req = urllib.request.Request(
        GRANOLA_API + path,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
            "User-Agent": UA,
            "X-Client-Version": "6.2.0",
        },
    )
    try:
        r = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            if e.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        except Exception:
            pass
        raise RuntimeError(f"Granola API {path} {e.code}: {raw[:200]!r}")
    raw = r.read()
    if r.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def list_documents(limit: int = 1000) -> list[dict]:
    data = _api("/v2/get-documents", {"limit": limit, "offset": 0})
    return data.get("docs", [])


def get_transcript(document_id: str) -> list[dict]:
    return _api("/v1/get-document-transcript", {"document_id": document_id})


# ── Formatting ──────────────────────────────────────────────────────────────


def slugify(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")[:60]


def extract_participants(doc: dict) -> list[str]:
    out = []
    people = doc.get("people") or {}
    creator = people.get("creator") or {}
    if isinstance(creator, dict):
        nm = (creator.get("details") or {}).get("person", {}).get("name", {}).get(
            "fullName"
        ) or creator.get("name")
        em = creator.get("email")
        if nm:
            out.append(f"{nm} <{em}>" if em else nm)
        elif em:
            out.append(em)
    for a in people.get("attendees") or []:
        if not isinstance(a, dict):
            continue
        nm = (a.get("details") or {}).get("person", {}).get("name", {}).get(
            "fullName"
        )
        em = a.get("email")
        if nm and em:
            out.append(f"{nm} <{em}>")
        elif nm:
            out.append(nm)
        elif em:
            out.append(em)
    # dedup, preserve order
    seen, uniq = set(), []
    for p in out:
        if p in seen:
            continue
        seen.add(p)
        uniq.append(p)
    return uniq


def format_transcript_segments(segments: list[dict]) -> str:
    if not segments:
        return ""
    lines = []
    for seg in segments:
        if not seg.get("is_final"):
            continue
        src = seg.get("source") or ""
        # The microphone track is the operator; everything else is the far end.
        speaker = OPERATOR_DISPLAY_NAME if src == "microphone" else "Other"
        spk = seg.get("detected_speaker_name") or speaker
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"[{spk}] {text}")
    return "\n".join(lines)


def render_content(doc: dict, transcript_segments: list[dict]) -> str:
    title = doc.get("title") or "Untitled meeting"
    date_ymd = (doc.get("created_at") or "")[:10]
    participants = extract_participants(doc)
    parts = [
        f"# {title}",
        f"**Date:** {date_ymd}",
        f"**Participants:** {', '.join(participants) if participants else '(unknown)'}",
        f"**Source:** Granola (meeting_id: {doc['id']})",
    ]
    summary = doc.get("summary")
    if summary:
        parts.append(f"\n## Summary\n{summary}")
    overview = doc.get("overview")
    if overview:
        parts.append(f"\n## Overview\n{overview}")
    notes = doc.get("notes_markdown") or doc.get("notes_plain")
    if notes:
        parts.append(f"\n## Notes\n{notes}")
    transcript = format_transcript_segments(transcript_segments)
    if transcript:
        parts.append(f"\n## Transcript\n{transcript}")
    return "\n".join(parts)


# ── DB ──────────────────────────────────────────────────────────────────────


def _conn() -> sqlite3.Connection:
    c = _db.connect(DB, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    c.row_factory = sqlite3.Row
    return c


def _existing_by_meeting_id(conn: sqlite3.Connection) -> dict[str, str]:
    """meeting_id_or_prefix -> slug for any transcript with the url stamped.

    Two stamp formats coexist:
      granola:<full-uuid>     (new, this module's format)
      granola://<8-char-id>   (older, partial UUID prefix)
    We index by both the full id and the 8-char prefix so dedup works against
    both conventions without forcing a migration.
    """
    rows = conn.execute(
        "SELECT slug, url FROM reference_docs WHERE slug LIKE 'transcript-%' AND url LIKE 'granola%'"
    ).fetchall()
    have: dict[str, str] = {}
    for r in rows:
        u = r["url"] or ""
        # Strip 'granola:' or 'granola://' prefix
        ident = u.split("granola://", 1)[-1] if "granola://" in u else u.split("granola:", 1)[-1]
        ident = ident.strip()
        if not ident:
            continue
        have[ident] = r["slug"]                  # full id or short prefix as-is
        if len(ident) >= 8:
            have[ident[:8]] = r["slug"]          # also index by 8-char prefix
    return have


def _is_known(have: dict[str, str], meeting_id: str) -> str | None:
    """Return matching slug if meeting_id (or its 8-char prefix) is already stamped."""
    if meeting_id in have:
        return have[meeting_id]
    if meeting_id[:8] in have:
        return have[meeting_id[:8]]
    return None


def _existing_slugs(conn: sqlite3.Connection) -> set[str]:
    return set(
        r["slug"]
        for r in conn.execute("SELECT slug FROM reference_docs WHERE slug LIKE 'transcript-%'")
    )


def _resolve_slug(conn: sqlite3.Connection, base_slug: str) -> str:
    """Return base_slug or base_slug-2 / -3 ... if collisions exist."""
    if not conn.execute(
        "SELECT 1 FROM reference_docs WHERE slug = ?", (base_slug,)
    ).fetchone():
        return base_slug
    for n in range(2, 20):
        cand = f"{base_slug}-{n}"
        if not conn.execute(
            "SELECT 1 FROM reference_docs WHERE slug = ?", (cand,)
        ).fetchone():
            return cand
    raise RuntimeError(f"Slug collision exhausted for {base_slug}")


def backfill_meeting_ids(verbose: bool = True) -> int:
    """Walk Granola docs; for each, find an existing transcript that matches by
    date+slugified-title and stamp `url = granola:<uuid>`. One-time cleanup so
    future syncs dedup correctly."""
    docs = list_documents()
    conn = _conn()
    try:
        slugs = _existing_slugs(conn)
        # Index by (date, first-15-of-title-slug) for soft match on edited titles
        soft: dict[tuple[str, str], str] = {}
        for s in slugs:
            d = s[11:21]
            tail = s[22:]
            soft.setdefault((d, tail[:15]), s)
        stamped = 0
        for d in docs:
            date = (d.get("created_at") or "")[:10]
            title = d.get("title") or ""
            tslug = slugify(title)
            base = f"transcript-{date}-{tslug}"
            cand = base if base in slugs else None
            if not cand:
                # Try with collision suffixes
                for n in range(2, 10):
                    c = f"{base}-{n}"
                    if c in slugs:
                        cand = c
                        break
            if not cand:
                # Soft match: same date + 15-char prefix of slugified title
                cand = soft.get((date, tslug[:15]))
            if not cand:
                continue
            # If already stamped, skip
            row = conn.execute(
                "SELECT url FROM reference_docs WHERE slug = ?", (cand,)
            ).fetchone()
            if row and row["url"] and row["url"].startswith("granola:"):
                continue
            conn.execute(
                "UPDATE reference_docs SET url = ? WHERE slug = ?",
                (f"granola:{d['id']}", cand),
            )
            stamped += 1
            if verbose:
                print(f"  stamped {cand} -> {d['id']}")
        conn.commit()
        if verbose:
            print(f"backfill: stamped {stamped} row(s)")
        return stamped
    finally:
        conn.close()


def _insert_transcript(
    conn: sqlite3.Connection, doc: dict, transcript_segments: list[dict]
) -> str:
    title = doc.get("title") or "Untitled meeting"
    date_ymd = (doc.get("created_at") or "")[:10] or datetime.now().strftime("%Y-%m-%d")
    base_slug = f"transcript-{date_ymd}-{slugify(title)}"
    slug = _resolve_slug(conn, base_slug)
    content = render_content(doc, transcript_segments)
    conn.execute(
        """
        INSERT INTO reference_docs
            (slug, title, category, content, source_file, updated_at, url, doc_type, tags)
        VALUES (?, ?, 'transcript', ?, 'granola', datetime('now'), ?, 'meeting_transcript', 'granola,meeting')
        """,
        (slug, title, content, f"granola:{doc['id']}"),
    )
    return slug


def sync_all(since_days: int | None = None, fetch_transcript: bool = True, verbose: bool = True) -> dict:
    """Pull missing Granola docs into reference_docs. Dedup by meeting_id in url.

    Returns counts dict {'total': N, 'new': N, 'skipped': N, 'no_transcript': N}.
    """
    counts = {"total": 0, "new": 0, "skipped": 0, "no_transcript": 0, "errors": 0}
    docs = list_documents()
    counts["total"] = len(docs)
    if since_days is not None:
        cutoff = time.time() - since_days * 86400
        docs = [
            d
            for d in docs
            if datetime.fromisoformat(
                d["created_at"].replace("Z", "+00:00")
            ).timestamp()
            >= cutoff
        ]

    conn = _conn()
    try:
        have = _existing_by_meeting_id(conn)
        if verbose:
            print(
                f"granola sync: {counts['total']} docs in Granola, {len(have)} already stamped, processing {len(docs)} candidate(s)"
            )

        for d in docs:
            mid = d["id"]
            if _is_known(have, mid):
                counts["skipped"] += 1
                continue

            # Fetch transcript (skip silently if 0 segments)
            transcript_segments: list[dict] = []
            if fetch_transcript:
                try:
                    transcript_segments = get_transcript(mid)
                except Exception as e:
                    if verbose:
                        print(f"  transcript fetch failed {mid[:8]}: {e}")
                    counts["errors"] += 1

            if not transcript_segments:
                counts["no_transcript"] += 1
                # Still store the doc -- notes alone are valuable

            slug = _insert_transcript(conn, d, transcript_segments)
            counts["new"] += 1
            if verbose:
                segs = len(transcript_segments)
                print(
                    f"  + {slug} ({segs} segs, title={d.get('title','')[:40]!r})"
                )

        conn.commit()
    finally:
        conn.close()

    if verbose:
        print(
            f"granola sync done: new={counts['new']} skipped={counts['skipped']} "
            f"no_transcript={counts['no_transcript']} errors={counts['errors']}"
        )
    return counts


# ── CLI ─────────────────────────────────────────────────────────────────────


def cmd_list(args):
    docs = list_documents()
    conn = _conn()
    have = _existing_by_meeting_id(conn)
    slugs = _existing_slugs(conn)
    conn.close()
    print(
        f"Granola: {len(docs)} doc(s) | DB: {len(slugs)} transcript(s) | stamped: {len(have)}"
    )
    for d in sorted(docs, key=lambda x: x.get("created_at") or ""):
        mid = d["id"]
        date = (d.get("created_at") or "")[:10]
        title = (d.get("title") or "")[:60]
        if _is_known(have, mid):
            tag = "HAVE"
        elif f"transcript-{date}-{slugify(d.get('title') or '')}" in slugs:
            tag = "HAVE?"  # slug match but not stamped
        else:
            tag = "MISS"
        print(f"  {tag:5s} {date} {mid[:8]} | {title}")


def cmd_sync(args):
    sync_all(since_days=args.since, fetch_transcript=not args.no_transcript)


def cmd_backfill(args):
    backfill_meeting_ids()


def cmd_token(args):
    tok = get_access_token()
    print(tok[:80] + "...")


def main():
    p = argparse.ArgumentParser(description="Granola transcript sync")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list", help="List Granola docs vs DB")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("sync", help="Pull missing transcripts into DB")
    s.add_argument("--since", type=int, default=None, help="Only last N days")
    s.add_argument("--no-transcript", action="store_true", help="Skip transcript fetch (notes only)")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("backfill", help="Stamp granola:<uuid> into existing rows by slug match")
    s.set_defaults(func=cmd_backfill)

    s = sub.add_parser("token", help="Print refreshed access token")
    s.set_defaults(func=cmd_token)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
