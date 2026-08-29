"""The quality gate hook, run the way Claude Code runs it: JSON on stdin, exit code out."""
import json
import subprocess

import pytest

OUTBOUND_PATH = "C:/work/outbound/reply.html"


def _run_gate(py, env, repo_root, content, path=OUTBOUND_PATH, tool="Write"):
    payload = {"session_id": "pytest", "tool_name": tool,
               "tool_input": {"file_path": path, "content": content}}
    return subprocess.run([py, str(repo_root / "hooks" / "quality_gate.py")],
                          input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=120)


def test_banned_phrase_blocks_the_write(py, env, repo_root):
    """A banned phrase in outbound text exits 2 and names the phrase."""
    r = _run_gate(py, env, repo_root, "Hi Sam,\n\nI wanted to reach out about the venue.\n\nBest,\nJo")
    assert r.returncode == 2, r.stderr
    assert "I wanted to reach out" in r.stderr


def test_em_dash_blocks_the_write(py, env, repo_root):
    """An em dash in outbound text exits 2."""
    r = _run_gate(py, env, repo_root, "Hi Sam,\n\nThe venue is booked \u2014 see you Friday.\n\nBest,\nJo")
    assert r.returncode == 2, r.stderr
    assert "Em dash" in r.stderr


def test_clean_text_passes(py, env, repo_root):
    """Plain text with no banned phrase and no em dash exits 0."""
    r = _run_gate(py, env, repo_root, "Hi Sam,\n\nThe venue is booked for Friday at 3pm. Could you confirm the headcount by Wednesday?\n\nBest,\nJo")
    assert r.returncode == 0, r.stderr


def test_non_outbound_path_is_not_scanned(py, env, repo_root):
    """The same banned phrase in a code file is left alone."""
    r = _run_gate(py, env, repo_root, "# I wanted to reach out\nx = 1\n", path="C:/work/src/module.py")
    assert r.returncode == 0, r.stderr


def test_both_hooks_share_one_ban_list(py, env, repo_root):
    """quality_gate and safety_guard import the same list object from slop_rules."""
    code = ("import sys; sys.path.insert(0, r'%s'); "
            "import slop_rules, quality_gate, safety_guard; "
            "print(quality_gate.SLOP_BANNED is slop_rules.SLOP_BANNED and "
            "safety_guard.BANNED_PHRASES is slop_rules.SLOP_BANNED, len(slop_rules.SLOP_BANNED))"
            % str(repo_root / "hooks"))
    r = subprocess.run([py, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    flag, n = r.stdout.split()
    assert flag == "True" and int(n) >= 40
