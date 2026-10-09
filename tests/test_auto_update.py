"""deploy/auto-update.sh against a real git clone, with the system around it
stubbed: `systemctl` and `pip` only log what they were asked, and `curl`
answers /healthz ok unless the checked-out tree contains meshradio/broken."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "auto-update.sh"

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("git") is None,
    reason="the script targets the Pi's Linux userland",
)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


class Pi:
    """An origin, a working copy that pushes to it, and the Pi's clone."""

    def __init__(self, root: Path):
        self.root = root
        self.origin = root / "origin.git"
        self.work = root / "work"
        self.clone = root / "clone"
        self.calls = root / "calls.log"
        self.state = root / "state"
        bin_ = root / "bin"
        bin_.mkdir()
        stub(bin_ / "systemctl", f'echo "systemctl $*" >> {self.calls}\n')
        stub(bin_ / "sleep", "exec /bin/sleep 0.1\n")
        stub(bin_ / "curl", (
            f'[ -e {self.clone}/meshradio/broken ] && exit 22\n'
            'echo \'{"ok":true,"tracks":1}\'\n'
        ))
        self.env = {
            **os.environ, **GIT_ENV,
            "PATH": f"{bin_}:{os.environ['PATH']}",
            "MESHRADIO_DIR": str(self.clone),
            "MESHRADIO_USER": subprocess.check_output(["id", "-un"], text=True).strip(),
            "MESHRADIO_HEALTH_WAIT": "1",
            "STATE_DIRECTORY": str(self.state),
        }
        self.git(root, "init", "--quiet", "--bare", "--initial-branch=main", str(self.origin))
        self.git(root, "init", "--quiet", "--initial-branch=main", str(self.work))
        self.git(self.work, "remote", "add", "origin", str(self.origin))
        self.commit("meshradio/app.py", "v1")
        self.git(self.work, "push", "--quiet", "origin", "main")
        self.git(root, "clone", "--quiet", str(self.origin), str(self.clone))
        venv = self.clone / ".venv" / "bin"
        venv.mkdir(parents=True)
        stub(venv / "pip", f'echo "pip $*" >> {self.calls}\n')

    def git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV},
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def commit(self, path: str, content: str) -> str:
        file = self.work / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        self.git(self.work, "add", "-A")
        self.git(self.work, "commit", "--quiet", "-m", content)
        return self.git(self.work, "rev-parse", "HEAD")

    def ci_passes(self) -> None:
        """What pi-deploy.yml does once main's tests go green."""
        self.git(self.work, "push", "--quiet", "origin", "HEAD:main", "HEAD:pi-deploy")

    def run(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", str(SCRIPT)], env=self.env, capture_output=True, text=True,
        )

    def head(self) -> str:
        return self.git(self.clone, "rev-parse", "HEAD")

    def log(self) -> str:
        return self.calls.read_text() if self.calls.exists() else ""


@pytest.fixture
def pi(tmp_path):
    return Pi(tmp_path)


def test_no_deploy_branch_yet_is_quiet(pi):
    result = pi.run()
    assert result.returncode == 0
    assert "no pi-deploy branch" in result.stdout
    assert pi.log() == ""


def test_a_green_commit_is_installed_and_restarted(pi):
    target = pi.commit("meshradio/app.py", "v2")
    pi.ci_passes()
    result = pi.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert pi.head() == target
    assert f"pip install --quiet -e {pi.clone}[media,hw]" in pi.log()
    assert "systemctl restart meshradio" in pi.log()
    # Nothing new: a second run does nothing at all.
    pi.calls.unlink()
    assert pi.run().returncode == 0
    assert pi.log() == ""


def test_a_docs_only_change_skips_the_restart(pi):
    target = pi.commit("README.md", "docs")
    pi.ci_passes()
    result = pi.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert pi.head() == target
    assert pi.log() == ""


def test_an_unhealthy_commit_is_rolled_back_and_not_retried(pi):
    before = pi.head()
    bad = pi.commit("meshradio/broken", "boom")
    pi.ci_passes()
    result = pi.run()
    assert result.returncode == 1
    assert "rolling back" in result.stdout
    assert "it's healthy" in result.stdout
    assert pi.head() == before
    assert (pi.state / "bad-commit").read_text().strip() == bad
    assert pi.log().count("systemctl restart meshradio") == 2
    # The next run leaves the known-bad commit alone...
    pi.calls.unlink()
    assert pi.run().returncode == 0
    assert pi.head() == before and pi.log() == ""
    # ...and the next green commit goes ahead.
    pi.git(pi.work, "rm", "--quiet", "meshradio/broken")
    pi.git(pi.work, "commit", "--quiet", "-m", "fix")
    fixed = pi.git(pi.work, "rev-parse", "HEAD")
    pi.ci_passes()
    assert pi.run().returncode == 0
    assert pi.head() == fixed
    assert not (pi.state / "bad-commit").exists()


def test_local_edits_are_left_alone(pi):
    before = pi.head()
    (pi.clone / "meshradio" / "app.py").write_text("tinkering")
    pi.commit("meshradio/app.py", "v2")
    pi.ci_passes()
    result = pi.run()
    assert result.returncode == 1
    assert "local edits" in result.stdout
    assert pi.head() == before
    assert pi.log() == ""
