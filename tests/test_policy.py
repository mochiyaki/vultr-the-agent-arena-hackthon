import pytest

from app.llm import extract_json
from app.sandbox.base import ExecResult, SandboxPolicy, safe_workspace_path


@pytest.mark.parametrize("path", ["../etc/passwd", "/etc/passwd", "a/../../x", "~/.ssh/id_rsa", ""])
def test_workspace_escape_rejected(path):
    with pytest.raises(ValueError):
        safe_workspace_path(path)


@pytest.mark.parametrize("path,expected", [
    ("out.txt", "/workspace/out.txt"),
    ("./sub/x.py", "/workspace/sub/x.py"),
    ("/workspace/a/../b", "/workspace/b"),
])
def test_workspace_paths_normalised(path, expected):
    assert safe_workspace_path(path) == expected


def test_default_policy_is_containment_first():
    p = SandboxPolicy()
    assert p.network == "none"
    assert p.read_only_rootfs and p.drop_all_capabilities and p.no_new_privileges
    assert p.user == "65534:65534" and p.runtime == "runsc"


def test_exec_result_hashes_stdout():
    r = ExecResult("echo hi", 0, "hi\n", "", 3)
    assert r.stdout_sha256 == "98ea6e4f216f2fb4b69fff9b3a44842c38686ca685f3f55dc48c5d3fb1107be4"


def test_extract_json_handles_fences_and_prose():
    assert extract_json('```json\n[{"a":1}]\n```') == [{"a": 1}]
    assert extract_json('Sure! Here is the plan: {"passed": true, "x": [1,2]} thanks') == {"passed": True, "x": [1, 2]}
    with pytest.raises(ValueError):
        extract_json("no json here")
