"""Structural / rhythm AI-tells analyzer (warn-only layer for quality_gate.py).

Why this exists: a reply can pass a banned-phrase list with zero hits and still
read as model-written, because detectors and careful readers score structure
and rhythm, not vocabulary. This module checks the structural tells both key
on: markdown residue, bullets, triads, negative parallelism, sentence-length
variation, participle tails, contraction rate.

v2 signals:
  * rationale prose: sentences that argue WHY the ask is reasonable ("so that",
    "which means", "that way", "the reason"), warned at density only.
  * reassurance beats ("that means a lot", "if anything comes up", "happy to
    walk you through", "no pressure").
  * hedge stacks, filler ("just" x2+, actually, basically, kind of, sort of),
    wordy phrases with their replacements, exclamation clusters, lift words,
    ad-copy tagline closers.
  * register: 'internal' (chat, no greeting expected) or 'external' (mail),
    auto-detected from a greeting line when not given. Internal skips the
    no-first-person and no-contractions checks.
  * words per concrete anchor (numbers, dates, links); warns above 25 on 80+
    words.

Design rules:
  * Pure function, no DB, no network. Never raises (callers wrap anyway).
  * WARN only. Statistical tells false-positive on non-native and very plain
    prose, so the hook prints and exits 0.
  * Works on plain text or HTML (tags stripped first).

CLI:  python slop_stats.py [--register internal|external] <file.txt> [...]
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
_GREETING_RE = re.compile(r"^(hi|hey|hello|dear)\b[^.!?]{0,40}[,:]?$", re.I)


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


def has_greeting(text: str) -> bool:
    lines = body_lines(text)
    return bool(lines) and bool(_GREETING_RE.match(lines[0]))


def sentences(text: str) -> list[str]:
    """Split prose into sentences, dropping greeting/sign-off/signature lines."""
    lines = body_lines(text)
    keep = []
    for l in lines:
        if _GREETING_RE.match(l):
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
    # the style guide: "not just X, Y" (comma form, no 'but')
    re.compile(r"\bnot just [\w'’-]+(\s[\w'’-]+){0,3},\s+(a|an|the)?\s?[\w'’-]+", re.I),
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

# v2: rationale prose. A clause that argues WHY the ask is reasonable or WHAT the
# benefit is. One per message is normal human writing ("so I can get you a meal");
# a message where most sentences carry one is the pattern the incident log caught.
RATIONALE = re.compile(
    r"(?:(?:,|\band|\bbut)\s+so (?:that|we|you|they|the|your|our|his|her|everyone|nothing|everything|it'?s|I can|I have|I'll)\b|"
    r"\bso that\b|\bwhich (?:means|lets|gives|keeps|makes)\b|\bthat (?:way|lets|gives|keeps|makes)\b|\bthis (?:way|helps|keeps|lets)\b|"
    r"\bthe reason\b|\bwhich is why\b|\bthat'?s why\b|\bthis is why\b|\bin order to\b|\bto make sure\b|\bto ensure\b|"
    r"\bbecause\b|\bgiven that\b|\bas a result\b|\bhelps (?:us|you|me|them)\b|\bthis is (?:me|us) asking\b|"
    r"\bso this is\b|\bthis is (?:just|only) (?:to|so)\b|\bthe (?:idea|point|goal) (?:is|being|here)\b)", re.I)

# v2: reassurance / validation beats (the "warm cushion" sentences the model adds
# around an ask; the style guide keeps a few of these for peers, our ban list already
# kills the worst; this layer warns on density).
REASSURE = re.compile(
    r"\b(if (something|anything) (comes up|changes|else comes up)|no pressure|no rush|no worries|"
    r"whenever works|totally understand|completely understand|fully understand|"
    r"happy to (talk|walk|help|chat|discuss|jump|hop|answer|clarify)|that means a lot|means a lot|"
    r"glad (you|to)|great to (hear|see|have)|appreciate you|i'?ll reach out|feel free to|"
    r"you'?re all set|rest assured|don'?t worry|not a problem|of course|absolutely|"
    r"looking forward to (it|this|working)|thanks (so much )?for (your patience|understanding|flagging|thinking of))\b", re.I)

# the style guide: hedge stacks and apologetic starters
HEDGE = re.compile(
    r"\b(i think maybe|maybe we could possibly|could possibly|might not be the best|"
    r"sorry if this is|i was wondering if|would it be possible to maybe|sorry to bother|"
    r"just checking in|i wanted to quickly|quick check[- ]in|this might be a dumb|"
    r"i feel like maybe|not (totally|entirely|100%) sure but)\b", re.I)

# the style guide: filler words (warn when "just" appears twice or the others at all)
FILLER = re.compile(r"\b(just|actually|basically|kind of|sort of|really|super|totally)\b", re.I)

# the style guide: wordy phrase -> replacement
WORDY = [
    (re.compile(r"\bi wanted to quickly check in if\b", re.I), "could you let me know if"),
    (re.compile(r"\bjust checking in to see\b", re.I), "following up on"),
    (re.compile(r"\bi think we should probably\b", re.I), "we should"),
    (re.compile(r"\bit would be great if we could\b", re.I), "could we"),
    (re.compile(r"\bas per (our|my) (discussion|conversation|email|last)\b", re.I), "as we discussed"),
    (re.compile(r"\bat this point in time\b", re.I), "now"),
    (re.compile(r"\bdue to the fact that\b", re.I), "because"),
    (re.compile(r"\bin order to\b", re.I), "to"),
    (re.compile(r"\bin regards to\b", re.I), "regarding"),
    (re.compile(r"\bon behalf of\b", re.I), "for (unless it is a formal representation)"),
    (re.compile(r"\bat the same\b(?! time)", re.I), "at the same time (incomplete phrase)"),
]

# the style guide: ad-copy closers / taglines as the last line
TAGLINE_CLOSER = re.compile(
    r"^(let'?s (make|build|do) (it|this) happen|onwards|here'?s to|to the next|"
    r"excited (for|about) what'?s (next|ahead)|can'?t wait to see|the best is yet|"
    r"big things ahead|stay tuned)\b", re.I)

# compact excess-vocabulary list (Kobak et al. 2025 top style words + the detector list +
# the style guide's "lift words"); the SLOP_BANNED phrase list in quality_gate.py still
# owns the hard bans.
EXCESS_VOCAB = re.compile(
    r"\b(delv(e|es|ing)|underscor(e|es|ing)|showcas(e|es|ing)|pivotal|crucial|notably|intricate|"
    r"realm|tapestry|testament|landscape|vibrant|meticulous(ly)?|foster(s|ing)?|leverag(e|es|ing)|"
    r"seamless(ly)?|robust|comprehensive|holistic|navigate|navigating|elevate|empower(s|ing)?|"
    r"unlock(s|ing)?|invaluable|insightful|nuanced|multifaceted|paramount|streamlin(e|ed|ing)|"
    r"transformative|groundbreaking|cutting-edge|game-?changer|synerg(y|ies)|"
    r"resonat(e|es|ed|ing)|spark(s|ing)? (a )?(conversation|discussion)|"
    r"at the forefront|in today'?s (fast-paced|ever-evolving|rapidly)|ever-evolving|"
    r"hardened|sharpest|dialed[- ]in|world-class|best-in-class|top-notch|stellar|turnkey|"
    r"supercharge[sd]?|laser-focused|battle-tested)\b", re.I)

MARKDOWN_RESIDUE = [
    (re.compile(r"\*\*[^*\n]{1,80}\*\*"), "bold asterisks **like this**"),
    (re.compile(r"(?m)^#{1,6}\s+\S"), "markdown heading (#)"),
    (re.compile(r"`[^`\n]{1,60}`"), "backtick code span"),
    (re.compile(r"(?m)^\s*[-*•]\s+\S"), "bullet list line"),
    (re.compile(r"(?m)^\s*\d+\.\s+\S.*\n\s*\d+\.\s+\S"), "numbered list block"),
]
BOLD_PHRASE = re.compile(r"\*\*[^*\n]{1,80}\*\*")

ODD_UNICODE = re.compile(r"[→←↑↓⇒✓✔✅❌⚡🚀💡🔥📌🎯•▪◦≈≠≤≥…]")

CONTRACTION = re.compile(r"\b\w+['’](t|s|re|ve|ll|d|m)\b", re.I)

# a "specific": something the model could not have made up from the ask alone
SPECIFIC = re.compile(
    r"(\d|https?://|www\.|@|\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\b|"
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b)")


# ---------------------------------------------------------------------------
# analyzer
# ---------------------------------------------------------------------------
def analyze(raw: str, register: str | None = None) -> dict:
    """Return {"score": int 0-100, "words": n, "sentences": n, "register": str,
    "hits": [(tag, weight, detail), ...]}.

    register: 'internal' | 'external' | None (auto: greeting line present -> external)."""
    text = strip_quoted(strip_html(raw or ""))
    if register not in ("internal", "external"):
        register = "external" if has_greeting(text) else "internal"
    sents = sentences(text)
    words = _words(" ".join(sents))
    n_words = len(words)
    hits: list[tuple[str, int, str]] = []

    if n_words < 25:
        return {"score": 0, "words": n_words, "sentences": len(sents), "register": register,
                "hits": hits, "note": "too short to judge (<25 words)"}

    # 1. markdown / formatting residue (the detector 12x, bullets 9x)
    for rx, label in MARKDOWN_RESIDUE:
        m = rx.search(text)
        if m:
            hits.append(("markdown", 20, f"{label}: {m.group(0).strip()[:60]!r}"))
    bolds = BOLD_PHRASE.findall(text)
    if len(bolds) >= 2:
        hits.append(("bold-stack", 8, f"{len(bolds)} bolded phrases (the style guide: bolding several phrases reads as ad copy)"))
    if ODD_UNICODE.search(text):
        hits.append(("unicode", 8, f"decorative/unusual character {ODD_UNICODE.search(text).group(0)!r}"))

    # 2. negative parallelism (the detector 3x, 'the most famous AI tic')
    for rx in NEG_PARALLEL:
        for m in rx.finditer(text):
            hits.append(("neg-parallel", 15, m.group(0).strip()[:90]))

    for rx in ANTITHESIS:
        for m in rx.finditer(text):
            hits.append(("antithesis", 8, m.group(0).strip()[:90]))

    # 3. tricolons (the detector 4x); density matters, not presence
    tri = [m.group(0) for m in TRICOLON.finditer(text)]
    if tri:
        per200 = len(tri) * 200 / max(n_words, 1)
        # a single "X, Y, and Z" is often a real list (tracks, dates); density is the tell
        w = 15 if (len(tri) >= 2 and per200 >= 2) else 8
        hits.append(("tricolon", w, f"{len(tri)} in {n_words} words: " + " | ".join(t[:60] for t in tri[:3])))

    # 4. participle tails (Reinhart 2025: 2-5x human rate)
    for m in PARTICIPLE_TAIL.finditer(text):
        hits.append(("participle-tail", 10, m.group(0).strip()[:90]))

    # 5. capstone openers / reframe-as-validation / fragment closer / tagline closer
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
        if TAGLINE_CLOSER.match(last):
            hits.append(("tagline-closer", 10, last[:90]))

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

    # 7. register: contractions and first person (arXiv 2604.23471 features).
    #    Internal messages are short and often have neither; only judge external mail.
    if n_words >= 80 and register == "external":
        c = len(CONTRACTION.findall(text))
        if c == 0:
            hits.append(("no-contractions", 8, f"0 contractions in {n_words} words"))
        fp = len(re.findall(r"\b(I|I'm|I've|I'll|I'd|me|my)\b", text))
        if fp == 0:
            hits.append(("no-first-person", 6, f"no first person in {n_words} words"))

    # 8. excess vocabulary (Kobak 2025 / the detector / the style guide lift words); phrase bans live in quality_gate
    ev = sorted({m.group(0).lower() for m in EXCESS_VOCAB.finditer(text)})
    if ev:
        hits.append(("excess-vocab", min(5 * len(ev), 20), ", ".join(ev[:8])))

    # 9. "every sentence is a beat": many short polished sentences with no specifics
    if n_words >= 60 and not SPECIFIC.search(" ".join(sents)):
        hits.append(("no-specifics", 6, "no number, date, day, or link anywhere in the body"))

    # 10. v2: rationale prose. Density, not presence.
    rat_sents = [s for s in sents if RATIONALE.search(s)]
    if len(sents) >= 4 and len(rat_sents) >= 3 and len(rat_sents) / len(sents) >= 0.4:
        hits.append(("rationale-prose", 15,
                     f"{len(rat_sents)} of {len(sents)} sentences justify the ask: "
                     + " | ".join(s[:70] for s in rat_sents[:2])
                     + "  (cut the why, keep the what: status + list + one-line ask)"))
    elif len(RATIONALE.findall(" ".join(sents))) >= 4:
        hits.append(("rationale-prose", 8, f"{len(RATIONALE.findall(' '.join(sents)))} justifying clauses in {n_words} words"))

    # 11. v2: reassurance / validation beats. One is warmth; two or more is cushioning.
    reas = [m.group(0) for m in REASSURE.finditer(text)]
    if len(reas) >= 2:
        hits.append(("reassurance", 6 * min(len(reas), 3), f"{len(reas)} cushion phrases: " + ", ".join(dict.fromkeys(r.lower() for r in reas))))

    # 12. v2: the style guide mechanics: hedges, filler, wordy phrases, exclamation clusters
    for m in HEDGE.finditer(text):
        hits.append(("hedge", 8, m.group(0)))
    fillers = [m.group(0).lower() for m in FILLER.finditer(text)]
    n_just = fillers.count("just")
    others = [f for f in fillers if f != "just"]
    if n_just >= 2 or len(others) >= 2 or (n_just + len(others)) >= 3:
        hits.append(("filler", 6, f"'just' x{n_just}" + (", " + ", ".join(dict.fromkeys(others)) if others else "")))
    for rx, fix in WORDY:
        m = rx.search(text)
        if m:
            hits.append(("wordy", 5, f"{m.group(0)!r} -> {fix}"))
    excl = text.count("!")
    if n_words >= 40 and excl * 100 / n_words >= 4:
        hits.append(("exclamation-cluster", 8, f"{excl} exclamation marks in {n_words} words"))
    if re.search(r"\b[A-Z]{4,}\b(?![-/])", " ".join(s for s in sents if not SPECIFIC.search(s))) and \
            len(re.findall(r"\b[A-Z]{4,}\b", text)) >= 2 and register == "external":
        pass  # acronyms are normal in our mail (AoE, MILP, EOD); no all-caps signal for now

    # 13. v2: human-length hint. Words per specific; long prose with few anchors is
    #     the shape that fails detectors and readers alike.
    specifics = len(set(SPECIFIC.findall(" ".join(sents))))
    if n_words >= 80 and specifics and n_words / specifics > 25:
        hits.append(("thin-on-specifics", 6, f"{n_words} words for {specifics} concrete anchors (>25 per anchor)"))

    score = min(100, sum(w for _, w, _ in hits))
    return {"score": score, "words": n_words, "sentences": len(sents), "register": register, "hits": hits}


def format_report(res: dict, label: str = "") -> str:
    head = f"STRUCTURE CHECK{(' ' + label) if label else ''}: score {res['score']}/100 " \
           f"({res['words']} words, {res['sentences']} sentences, {res.get('register', '?')})"
    if res.get("note"):
        return head + f"  [{res['note']}]"
    if not res["hits"]:
        return head + "  clean"
    lines = [head]
    for tag, w, detail in sorted(res["hits"], key=lambda h: -h[1]):
        lines.append(f"  - [{tag} +{w}] {detail}")
    return "\n".join(lines)


if __name__ == "__main__":
    args = sys.argv[1:]
    reg = None
    if args and args[0] == "--register" and len(args) >= 2:
        reg = args[1]
        args = args[2:]
    for path in args:
        with open(path, encoding="utf-8") as fh:
            print(format_report(analyze(fh.read(), register=reg), path.replace("\\", "/").split("/")[-1]))
