"""The structural linter: quiet on plain human text, loud on cushioned model prose."""
import importlib.util
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "platform" / "hooks"


@pytest.fixture(scope="module")
def slop_stats():
    spec = importlib.util.spec_from_file_location("slop_stats", HOOKS / "slop_stats.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PLAIN = ("Hey Sam, the venue confirmed for Friday 3pm, capacity 40. Two things from you: "
         "the final headcount by Wednesday and whether Priya still needs the projector. "
         "I'll send the address to everyone Thursday morning.")

CUSHIONED = ("Hi Sam,\n\nThanks so much for flagging this, that means a lot. I am sending you the six projects by the "
             "same author, so that one reader gives us a consistent read, which means the scores line up. That way the "
             "top five stay comparable. This is me asking because you told me four and delivered five. If anything comes up, "
             "feel free to reach out, happy to walk you through it. No pressure at all.\n\nBest,\nJo")


def test_plain_human_message_scores_low(slop_stats):
    """A short factual message with anchors gets a near-zero score."""
    res = slop_stats.analyze(PLAIN)
    assert res["score"] <= 10, res


def test_cushioned_email_is_flagged_for_reassurance_and_rationale(slop_stats):
    """Four cushion phrases and three purpose clauses trip both v2 signals."""
    res = slop_stats.analyze(CUSHIONED)
    tags = {h[0] for h in res["hits"]}
    assert "reassurance" in tags, res
    assert "rationale-prose" in tags, res
    assert res["score"] >= 30


def test_register_autodetects_external_from_greeting(slop_stats):
    """A greeting line marks the text as external mail."""
    assert slop_stats.analyze(CUSHIONED)["register"] == "external"


def test_register_autodetects_internal_without_greeting(slop_stats):
    """No greeting line means the internal (chat) register."""
    assert slop_stats.analyze(PLAIN)["register"] == "internal"


def test_explicit_register_overrides_detection(slop_stats):
    """A caller-supplied register wins over auto-detection."""
    assert slop_stats.analyze(PLAIN, register="external")["register"] == "external"


def test_short_text_is_not_judged(slop_stats):
    """Under 25 words the analyzer abstains instead of guessing."""
    res = slop_stats.analyze("Got it, thanks. See you Friday.")
    assert res["score"] == 0 and "too short" in res.get("note", "")


def test_markdown_residue_is_a_hit(slop_stats):
    """Bold asterisks in prose that will be sent as text are flagged."""
    res = slop_stats.analyze(PLAIN.replace("Two things", "**Two things**"))
    assert any(h[0] == "markdown" for h in res["hits"])
