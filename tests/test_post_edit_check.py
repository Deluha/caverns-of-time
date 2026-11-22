# .claude/hooks/post_edit_check.py: which edited file the hook checks (a repo .py
# path only), the `failures` decision order (ruff output first, a missing ruff
# tolerated, then the conventions check) and its exit codes — 0 on anything
# unexpected, 2 with the failure text on stderr when a check fails.

import importlib.util
import io
import json
import pathlib
import subprocess

_SPEC = importlib.util.spec_from_file_location(
    "post_edit_check", pathlib.Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "post_edit_check.py"
)
hook = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(hook)


def _payload(path):
    return json.dumps({"tool_input": {"file_path": path}})


def _repo_path(name):
    return str(pathlib.Path(hook.REPO_ROOT) / name)


def test_repo_python_file_is_returned():
    path = _repo_path("dev_tools/verify.py")
    assert hook.edited_python_file(_payload(path)) == path


def test_other_inputs_are_ignored():
    for raw in (
        _payload(_repo_path("README.md")),  # not .py
        _payload("/elsewhere/outside_repo.py"),  # outside the repo
        json.dumps({"tool_input": {}}),  # missing key
        "not json",
        _payload(_repo_path("dev_tools/a\0.py")),  # NUL byte
    ):
        assert hook.edited_python_file(raw) is None


def test_main_returns_0_on_garbage_stdin(monkeypatch):
    for raw in ("", "not json", "[" * 5000 + "]" * 5000, _payload("\ud800.py")):
        monkeypatch.setattr(hook.sys, "stdin", io.StringIO(raw))
        assert hook.main() == 0


def test_main_returns_2_and_writes_stderr_on_a_failure(monkeypatch):
    monkeypatch.setattr(hook, "failures", lambda path: "F401 unused import")
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO(_payload(_repo_path("dev_tools/verify.py"))))
    err = io.StringIO()
    monkeypatch.setattr(hook.sys, "stderr", err)
    assert hook.main() == 2
    assert "F401 unused import" in err.getvalue()


def _fake_run(ruff, conventions, calls):
    def run(command):
        calls.append(command)
        result = ruff if "ruff" in command else conventions
        return subprocess.CompletedProcess(command, result[0], result[1], result[2])

    return run


def test_failures_returns_ruff_output_without_the_conventions_check(monkeypatch):
    calls = []
    monkeypatch.setattr(hook, "run", _fake_run((1, "F401 unused import\n", ""), (0, "", ""), calls))
    assert hook.failures("a.py") == "F401 unused import\n"
    assert len(calls) == 1


def test_failures_tolerates_a_missing_ruff_and_lets_conventions_decide(monkeypatch):
    monkeypatch.setattr(hook, "run", _fake_run((1, "", "No module named ruff"), (0, "", ""), []))
    assert hook.failures("a.py") == ""


def test_failures_returns_the_conventions_output_when_ruff_passes(monkeypatch):
    monkeypatch.setattr(hook, "run", _fake_run((0, "", ""), (1, "C2 print() in package code\n", ""), []))
    assert hook.failures("a.py") == "C2 print() in package code\n"
