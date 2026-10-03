import contextlib
import subprocess
from pathlib import Path

import pytest

from review_agent.review import snapshot


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _ = git(tmp_path, "init", "-q")
    _ = git(tmp_path, "config", "user.email", "t@example.com")
    _ = git(tmp_path, "config", "user.name", "t")
    for text in ("reviewed\n", "later\n"):
        _ = (tmp_path / "app.py").write_text(text)
        _ = git(tmp_path, "add", "app.py")
        _ = git(tmp_path, "commit", "-q", "-m", text.strip())
    _ = (tmp_path / "app.py").write_text("uncommitted\n")
    return tmp_path


def test_reads_the_reviewed_commit_not_the_working_tree(repo: Path):
    with snapshot(str(repo), "HEAD~1") as tree:
        assert (Path(tree) / "app.py").read_text() == "reviewed\n"
    assert (repo / "app.py").read_text() == "uncommitted\n"  # the user's checkout is untouched


@pytest.mark.parametrize("fail", [False, True])
def test_worktree_is_removed_afterwards(repo: Path, fail: bool):
    path = None
    with pytest.raises(RuntimeError) if fail else contextlib.nullcontext():
        with snapshot(str(repo), "HEAD") as tree:
            path = Path(tree)
            assert path.exists()
            if fail:
                raise RuntimeError("review crashed")
    assert path is not None and not path.exists()
    assert git(repo, "worktree", "list").count("\n") == 1


def test_unknown_commit_is_a_clear_error(repo: Path):
    with pytest.raises(ValueError, match="can't check out"):
        with snapshot(str(repo), "no-such-ref"):
            pass
