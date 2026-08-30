"""The install self-check passes end to end against a fresh database."""
import subprocess


def test_selfcheck_passes_nine_of_nine(py, env, repo_root):
    """platform/tools/selfcheck.py exits 0 and reports every check passed."""
    r = subprocess.run([py, str(repo_root / "platform" / "tools" / "selfcheck.py")],
                       capture_output=True, text=True, env=env, timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "selfcheck: 9/9 passed" in r.stdout


def test_init_db_is_idempotent(py, env, repo_root):
    """Re-running init_db on an initialised data dir refuses to clobber it."""
    r = subprocess.run([py, str(repo_root / "platform" / "db" / "init_db.py")],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "already initialized" in r.stdout or "init complete" in r.stdout
