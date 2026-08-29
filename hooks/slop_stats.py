"""Structural / rhythm AI-tells analyzer (warn-only layer for quality_gate.py).

Why this exists: a phrase blacklist catches vocabulary, not rhythm. Detectors
(and careful human readers) key on structure: markdown residue in prose,
bullet walls, tricolons, negative parallelism, participle tails, uniform
sentence length, zero contractions. This module scores those tells using the
signal families Pangram published per-10k-word rates for (markdown residue
12x, bullets 9x, triads 4x, negative parallelism 3x) plus the stylometric
features from Reinhart et al. PNAS 2025 and arXiv 2604.23471 (sentence-length
CV, participle tails, contraction rate), with thresholds borrowed from the
dslop / sloplint / deslop rule sets.

Design rules:
  * Pure function, no DB, no network. Never raises (callers wrap anyway).
  * WARN only. Statistical tells false-positive on non-native and very plain
    prose (Liang et al. 2023), so the hook prints and exits 0.
  * Works on plain text or HTML (tags stripped first).

CLI:  python slop_stats.py <file.txt> [...]   -> report per file
"""
from __future__ import annotations

import html as _html
import re
import statistics
import sys

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_SIGNOFF_RE = re.compile(
    r"^(best|thanks|thank you|cheers|regards|kind regards|warm regards|all the best|"
    r"talk soon|see you (there|soon))[,!.]?$", re.I)


def strip_html(text: str) -> str:
    if "<" in text and ">" in text:
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", text)
        text = _TAG_RE.sub(" ", text)
        text = _html.unescape(text)
    return text


def strip_quoted(text: str) -> str:
    """Drop quoted reply history (lines starting with '>' and 'On ... wrote:' tails)."""
    m = re.search(r"\n\s*On .{5,120} wrote:\s*\n", text)
    if m:
        text = text[: m.start()]
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))


def body_lines(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines() if l.strip()]


def sentences(text: str) -> list[str]:
    """Split prose into sentences, dropping greeting/sign-off/signature lines."""
    lines = body_lines(text)
    keep = []
    for l in lines:
        if re.match(r"^(hi|hey|hello|dear)\b[^.!?]{0,40}[,:]?$", l, re.I):
            continue
        if _SIGNOFF_RE.match(l):
            continue
        if len(l.split()) <= 3 and not re.search(r"[.!?]$", l):
            continue  # name line / signature fragment
        keep.append(l)
    flat = _WS_RE.sub(" ", " ".join(keep))
    parts = re.split(r"(?<=[.!?])\s+(?=[\"'(A-Z0-9])", flat)
    return [p.strip() for p in parts if len(p.strip().split()) >= 1]


def _words(s: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z'’-]*", s)


# ---------------------------------------------------------------------------
# pattern tables
# ---------------------------------------------------------------------------
NEG_PARALLEL = [
    re.compile(r"\bnot (just|only|merely|simply|about)\b[^.;:]{2,80}?\b(but|it'?s|it is|rather)\b", re.I),
    re.compile(r"\b(isn'?t|is not|wasn'?t|aren'?t) (about|just|only|a matter of)\b[^.;:]{2,80}?,\s*(it'?s|it is|but)\b", re.I),
    re.compile(r"\bit'?s not (a|an|about|that)\b[^.;:]{2,80}?,\s*it'?s\b", re.I),
    re.compile(r"\bless (about|of)\b[^.;:]{2,60}?\bmore (about|of)\b", re.I),
    re.compile(r"\bnot because\b[^.;:]{2,80}?\bbut because\b", re.I),
]

# antithesis / contrast beats: "tell a thorough report from a fluent one", "X, not Y.",
# "more X than Y", "the difference between X and Y is"
ANTITHESIS = [
    re.compile(r"\b(tell|distinguish|separate|know)\s+(a|an|the)?\s?[\w-]+(\s[\w-]+)?\s+from\s+(a|an|the)\s+[\w-]+(\s+one)?\b", re.I),
    re.compile(r"\b[\w'’-]+,\s+not\s+[\w'’-]+[.!]", re.I),
    re.compile(r"\bit'?s\s+(a|an)\s+[\w-]+,\s+not\s+(a|an)\s+[\w-]+\b", re.I),
    re.compile(r"\b(more|less)\s+[\w-]+\s+than\s+[\w-]+\b[^.]{0,20}[.!]", re.I),
]

# "X, Y, and Z" with short items (1-4 words each)
_ITEM = r"[A-Za-z][\w'’-]*(?: [\w'’-]+){0,3}"
TRICOLON = re.compile(rf"\b({_ITEM}), ({_ITEM}), (?:and|or) ({_ITEM})\b")

PARTICIPLE_TAIL = re.compile(
    r",\s+(underscoring|highlighting|emphasizing|emphasising|reflecting|cementing|showcasing|"
    r"demonstrating|ensuring|reinforcing|signaling|signalling|marking|solidifying|shaping|"
    r"paving|fostering|driving|enabling|empowering|positioning|setting the stage|"
    r"leaving|making it|helping to|allowing for)\b[^.!?]{0,120}[.!?]", re.I)

CAPSTONE_OPENER = re.compile(
    r"^(in summary|ultimately|overall|in short|at its core|the bottom line|"
    r"in the end|all in all|to sum up|in conclusion|the takeaway)\b", re.I)

REFRAME = [
    re.compile(r"\b(what you (spotted|noticed|raised|flagged|caught|saw)|your (point|feedback|concern|pushback|note))"
               r"\b[^.]{0,40}\bis (the whole|precisely|the very|the exact|the entire|the real)\b", re.I),
    re.compile(r"\b(that'?s|this is) (a )?(fair|great|good|valid|important) (point|question|catch|call)\b", re.I),
    re.compile(r"\byou'?re (absolutely |completely |totally )?right\b", re.I),
    re.compile(r"\bis (the )?(most|single most) (useful|valuable|important) thing\b", re.I),
]

# compact excess-vocabulary list (Kobak et al. 2025 top style words + Pangram list);
# the SLOP_BANNED phrase list in quality_gate.py still owns the hard bans.
EXCESS_VOCAB = re.compile(
    r"\b(delv(e|es|ing)|underscor(e|es|ing)|showcas(e|es|ing)|pivotal|crucial|notably|intricate|"
    r"realm|tapestry|testament|landscape|vibrant|meticulous(ly)?|foster(s|ing)?|leverag(e|es|ing)|"
    r"seamless(ly)?|robust|comprehensive|holistic|navigate|navigating|elevate|empower(s|ing)?|"
    r"unlock(s|ing)?|invaluable|insightful|nuanced|multifaceted|paramount|streamlin(e|ed|ing)|"
    r"transformative|groundbreaking|cutting-edge|game-?changer|synerg(y|ies)|"
    r"resonat(e|es|ed|ing)|spark(s|ing)? (a )?(conversation|discussion)|"
    r"at the forefront|in today'?s (fast-paced|ever-evolving|rapidly)|ever-evolving)\b", re.I)

MARKDOWN_RESIDUE = [
    (re.compile(r"\*\*[^*\n]{1,80}\*\*"), "bold asterisks **like this**"),
    (re.compile(r"(?m)^#{1,6}\s+\S"), "markdown heading (#)"),
    (re.compile(r"`[^`\n]{1,60}`"), "backtick code span"),
    (re.compile(r"(?m)^\s*[-*•]\s+\S"), "bullet list line"),
    (re.compile(r"(?m)^\s*\d+\.\s+\S.*\n\s*\d+\.\s+\S"), "numbered list block"),
]

ODD_UNICODE = re.compile(r"[→←↑↓⇒✓✔✅❌⚡🚀💡🔥📌🎯•▪◦≈≠≤≥…]")

CONTRACTION = re.compile(r"\b\w+['’](t|s|re|ve|ll|d|m)\b", re.I)


# ---------------------------------------------------------------------------
# analyzer
# ---------------------------------------------------------------------------
def analyze(raw: str) -> dict:
    """Return {"score": int 0-100, "words": n, "sentences": n, "hits": [(tag, weight, detail), ...]}."""
    text = strip_quoted(strip_html(raw or ""))
    sents = sentences(text)
    words = _words(" ".join(sents))
    n_words = len(words)
    hits: list[tuple[str, int, str]] = []

    if n_words < 25:
        return {"score": 0, "words": n_words, "sentences": len(sents), "hits": hits,
                "note": "too short to judge (<25 words)"}

    # 1. markdown / formatting residue (Pangram 12x, bullets 9x)
    for rx, label in MARKDOWN_RESIDUE:
        m = rx.search(text)
        if m:
            hits.append(("markdown", 20, f"{label}: {m.group(0).strip()[:60]!r}"))
    if ODD_UNICODE.search(text):
        hits.append(("unicode", 8, f"decorative/unusual character {ODD_UNICODE.search(text).group(0)!r}"))

    # 2. negative parallelism (Pangram 3x, 'the most famous AI tic')
    for rx in NEG_PARALLEL:
        for m in rx.finditer(text):
            hits.append(("neg-parallel", 15, m.group(0).strip()[:90]))

    for rx in ANTITHESIS:
        for m in rx.finditer(text):
            hits.append(("antithesis", 8, m.group(0).strip()[:90]))

    # 3. tricolons (Pangram 4x); density matters, not presence
    tri = [m.group(0) for m in TRICOLON.finditer(text)]
    if tri:
        per200 = len(tri) * 200 / max(n_words, 1)
        # a single "X, Y, and Z" is often a real list (tracks, dates); density is the tell
        w = 15 if (len(tri) >= 2 and per200 >= 2) else 8
        hits.append(("tricolon", w, f"{len(tri)} in {n_words} words: " + " | ".join(t[:60] for t in tri[:3])))

    # 4. participle tails (Reinhart 2025: 2-5x human rate)
    for m in PARTICIPLE_TAIL.finditer(text):
        hits.append(("participle-tail", 10, m.group(0).strip()[:90]))

    # 5. capstone openers / reframe-as-validation / fragment closer
    for s in sents:
        if CAPSTONE_OPENER.match(s):
            hits.append(("capstone", 10, s[:90]))
    for rx in REFRAME:
        m = rx.search(text)
        if m:
            hits.append(("reframe", 12, m.group(0).strip()[:90]))
    if len(sents) >= 3:
        last = sents[-1]
        lw = len(_words(last))
        if 1 <= lw <= 5 and last.endswith((".", "!")) and not re.search(r"\?$", last) \
                and not re.match(r"^(thanks|thank you|see you|talk soon|let me know)", last, re.I):
            hits.append(("fragment-closer", 8, last))

    # 6. sentence-length rhythm: local CV over a 5-sentence window (dslop threshold 0.30)
    lens = [len(_words(s)) for s in sents]
    if len(lens) >= 6:
        cvs = []
        for i in range(0, len(lens) - 4):
            win = lens[i:i + 5]
            mu = statistics.mean(win)
            cvs.append(statistics.pstdev(win) / mu if mu else 0)
        if cvs and min(cvs) < 0.30:
            hits.append(("uniform-rhythm", 10, f"5-sentence window with length CV {min(cvs):.2f} (<0.30); lengths {lens}"))
    elif len(lens) >= 4:
        mu = statistics.mean(lens)
        if mu and statistics.pstdev(lens) / mu < 0.25:
            hits.append(("uniform-rhythm", 8, f"sentence lengths {lens}, CV {statistics.pstdev(lens)/mu:.2f}"))

    # 7. register: contractions and first person (arXiv 2604.23471 features)
    if n_words >= 80:
        c = len(CONTRACTION.findall(text))
        if c == 0:
            hits.append(("no-contractions", 8, f"0 contractions in {n_words} words"))
        fp = len(re.findall(r"\b(I|I'm|I've|I'll|I'd|me|my)\b", text))
        if fp == 0:
            hits.append(("no-first-person", 6, f"no first person in {n_words} words"))

    # 8. excess vocabulary (Kobak 2025 / Pangram lists); phrase bans live in quality_gate
    ev = sorted({m.group(0).lower() for m in EXCESS_VOCAB.finditer(text)})
    if ev:
        hits.append(("excess-vocab", min(5 * len(ev), 20), ", ".join(ev[:8])))

    # 9. "every sentence is a beat": many short polished sentences with no specifics
    if n_words >= 60 and not re.search(r"\b(\d{1,4}|[A-Z][a-z]+ \d|Mon|Tue|Wed|Thu|Fri|Sat|Sun|January|February|March|April|May|June|July|August|September|October|November|December)\b", text):
        hits.append(("no-specifics", 6, "no number, date, or day anywhere in the body"))

    score = min(100, sum(w for _, w, _ in hits))
    return {"score": score, "words": n_words, "sentences": len(sents), "hits": hits}


def format_report(res: dict, label: str = "") -> str:
    head = f"STRUCTURE CHECK{(' ' + label) if label else ''}: score {res['score']}/100 " \
           f"({res['words']} words, {res['sentences']} sentences)"
    if res.get("note"):
        return head + f"  [{res['note']}]"
    if not res["hits"]:
        return head + "  clean"
    lines = [head]
    for tag, w, detail in sorted(res["hits"], key=lambda h: -h[1]):
        lines.append(f"  - [{tag} +{w}] {detail}")
    return "\n".join(lines)


if __name__ == "__main__":
    for path in sys.argv[1:]:
        with open(path, encoding="utf-8") as fh:
            print(format_report(analyze(fh.read()), path.replace("\\", "/").split("/")[-1]))
