import pytest

from review_agent.diff import is_generated, number_lines, pack, parse_args, split_files

PATCH = """\
diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -10,3 +10,4 @@ def handler():
     a = 1
-    b = 2
+    b = 3
+    c = 4
     return a
diff --git a/util.py b/util.py
--- a/util.py
+++ b/util.py
@@ -1,1 +1,1 @@
-x = 1
+x = 2
"""


def test_new_file_lines_are_numbered_and_removed_lines_are_not():
    out = number_lines(PATCH).splitlines()
    assert "    10      a = 1" in out
    assert "       -    b = 2" in out
    assert "    11 +    b = 3" in out
    assert "    12 +    c = 4" in out
    assert "    13      return a" in out


def test_numbering_restarts_per_file_and_headers_stay_unnumbered():
    out = number_lines(PATCH).splitlines()
    assert "--- a/util.py" in out and "+++ b/util.py" in out
    assert "     1 +x = 2" in out


def test_split_files_keeps_git_order_and_each_files_own_lines():
    (app, app_diff), (util, util_diff) = split_files(PATCH)
    assert (app, util) == ("app.py", "util.py")
    assert app_diff.startswith("diff --git a/app.py") and "x = 2" not in app_diff
    assert util_diff.startswith("diff --git a/util.py") and "return a" not in util_diff


def test_pack_takes_whole_files_and_lists_the_rest():
    files = [("big.py", "x" * 50), ("small.py", "y" * 10), ("other.py", "z" * 10)]
    text, left_out = pack(files, budget=30)
    assert left_out == ["big.py"]
    assert "y" * 10 in text and "z" * 10 in text and "x" not in text


def test_is_generated_matches_basenames():
    assert all(map(is_generated, ["uv.lock", "web/package-lock.json", "api/user_pb2.py", "dist/app.min.js"]))
    assert not any(map(is_generated, ["lock.py", "src/locks.rs", "pyproject.toml", "app.js"]))


def test_valid_args_are_stripped():
    assert parse_args({"base": " main ", "head": "HEAD", "path": " app.py "}) == ({"base": "main", "head": "HEAD"}, "app.py")
    assert parse_args({"base": "HEAD~1", "head": "HEAD"}) == ({"base": "HEAD~1", "head": "HEAD"}, None)


@pytest.mark.parametrize("args, problem", [
    ({"head": "HEAD"}, "Missing 'base'"),
    ({"base": "main", "head": "  "}, "Missing 'head'"),
    ({"base": "--output=/tmp/x", "head": "HEAD"}, "not an option"),  # would reach git as a flag
    ({"base": "main", "head": " -p"}, "not an option"),
    ({"base": "main", "head": "HEAD", "path": "-x"}, "'path' must be"),
    ({"base": "main", "head": "HEAD", "path": ""}, "'path' must be"),
])
def test_bad_args_get_a_message_not_a_git_call(args: dict[str, object], problem: str):
    message = parse_args(args)
    assert isinstance(message, str) and problem in message
