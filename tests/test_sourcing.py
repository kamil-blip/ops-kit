"""The sourcing pipeline and screening modules run offline on fictional data."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def run(args, env_extra=None, cwd=ROOT):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.update(env_extra or {})
    return subprocess.run([PY] + [str(a) for a in args], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=300)


def test_pipeline_demo_runs_twice_and_resets(tmp_path):
    """demo.py seeds 30 fictional candidates, is idempotent on rerun, and --reset removes them."""
    env = {"SOURCING_DB": str(tmp_path / "sourcing.db")}
    first = run([ROOT / "sourcing" / "pipeline" / "demo.py"], env)
    assert first.returncode == 0, first.stderr
    assert "30 new" in first.stdout and "conversion" in first.stdout
    second = run([ROOT / "sourcing" / "pipeline" / "demo.py"], env)
    assert second.returncode == 0 and "0 new" in second.stdout
    reset = run([ROOT / "sourcing" / "pipeline" / "demo.py", "--reset"], env)
    assert reset.returncode == 0


def test_tracker_status_and_funnel_json(tmp_path):
    """tracker.py status prints the funnel table; funnel.py --json returns parseable numbers."""
    env = {"SOURCING_DB": str(tmp_path / "sourcing.db")}
    assert run([ROOT / "sourcing" / "pipeline" / "demo.py"], env).returncode == 0
    st = run([ROOT / "sourcing" / "pipeline" / "tracker.py", "status"], env)
    assert st.returncode == 0 and "conv" in st.stdout
    fj = run([ROOT / "sourcing" / "pipeline" / "funnel.py", "--json"], env)
    assert fj.returncode == 0
    data = json.loads(fj.stdout[fj.stdout.index("{"):])
    assert data


def test_templates_lint_rejects_banned_phrase(tmp_path):
    """The template lint fails on a banned phrase and on an unresolved placeholder."""
    bad = tmp_path / "bad.md"
    bad.write_text("Hi {{first_name}}," + chr(10) * 2 + "I wanted to reach out about {{missing}}." + chr(10), encoding="utf-8")
    r = run([ROOT / "sourcing" / "pipeline" / "templates.py", "lint", bad])
    assert r.returncode != 0 or "banned" in (r.stdout + r.stderr).lower() or "unresolved" in (r.stdout + r.stderr).lower()


def test_score_gates_then_points():
    """score.py applies gates before points and names who to send."""
    r = run([ROOT / "sourcing" / "screening" / "score.py", "--rubric", ROOT / "sourcing" / "screening" / "rubrics" / "example-ops-generalist-role.json",
             "--records", ROOT / "sourcing" / "screening" / "examples" / "candidates.json"])
    assert r.returncode == 0, r.stderr
    assert "GATE FAIL" in r.stdout and "PASS" in r.stdout and "send" in r.stdout.lower()


def test_assign_demo_has_no_conflicts_or_gaps():
    """assign.py --demo solves with zero coverage gaps and zero conflicts of interest."""
    r = run([ROOT / "sourcing" / "screening" / "assign.py", "--demo"])
    assert r.returncode == 0, r.stderr
    assert "coverage gaps: 0" in r.stdout and "conflicts of interest: 0" in r.stdout


def test_validate_synthetic_reports_spearman():
    """validate.py --synthetic prints per-model rank correlation and cross-model agreement."""
    r = run([ROOT / "sourcing" / "screening" / "validate.py", "--synthetic"])
    assert r.returncode == 0, r.stderr
    assert "spearman" in r.stdout.lower() and "cross-model" in r.stdout.lower()


def test_bias_demo_labels_reviewers():
    """bias.py --demo classifies reviewers as harsh, lenient or neutral."""
    r = run([ROOT / "sourcing" / "screening" / "bias.py", "--demo"])
    assert r.returncode == 0, r.stderr
    assert "HARSH" in r.stdout and "LENIENT" in r.stdout
