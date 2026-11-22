"""Tests for dev_tools/check_docs_sync.py: the pure classifier, the token rules and the git parsing."""

from __future__ import annotations

import pytest

from dev_tools import check_docs_sync as ds

SKILL = ".claude/skills/caverns-of-time-development/"


def _code(changed: set[str], messages: str = "", actor: str = "", exempt: frozenset[str] = frozenset()) -> int:
    return ds.evaluate(changed, messages, actor, exempt=exempt)[0]


def test_no_documented_area_passes() -> None:
    assert _code({"pyproject.toml", "ROADMAP.md"}) == 0


@pytest.mark.parametrize(
    ("code_file", "doc"),
    [
        ("caverns_of_time/nozdormu.py", "caverns_of_time/README.md"),
        ("dev_tools/verify.py", "dev_tools/README.md"),
        ("tests/test_x.py", "tests/README.md"),
        (".github/workflows/ci-tests.yml", SKILL + "references/qa-and-ci.md"),
        (".claude/agents/reviewer.md", SKILL + "references/recipes.md"),
        (".claude/hooks/session-start.sh", SKILL + "references/qa-and-ci.md"),
    ],
)
def test_area_needs_its_doc(code_file: str, doc: str) -> None:
    code, lines = ds.evaluate({code_file}, "")
    assert code == 1 and doc in "\n".join(lines)
    assert _code({code_file, doc}) == 0


@pytest.mark.parametrize("doc", ["CLAUDE.md", SKILL + "SKILL.md", SKILL + "references/conventions.md"])
def test_global_docs_satisfy_any_area(doc: str) -> None:
    assert _code({"caverns_of_time/nozdormu.py", doc}) == 0


def test_non_doc_skill_files_do_not_satisfy() -> None:
    assert _code({"caverns_of_time/nozdormu.py", SKILL + "evals/evals.json"}) == 1
    assert _code({"caverns_of_time/nozdormu.py", ".claude/skills/verify/SKILL.md"}) == 1


def test_other_areas_readme_does_not_satisfy() -> None:
    assert _code({"caverns_of_time/nozdormu.py", "dev_tools/README.md"}) == 1


def test_non_code_suffix_in_an_area_is_ignored() -> None:
    assert _code({"caverns_of_time/notes.txt", ".github/workflows/readme.txt", ".claude/agents/x.py"}) == 0
    assert ds.area_of("caverns_of_time/schema.sql") == "caverns_of_time"


def test_token_on_its_own_line_skips_and_inline_does_not() -> None:
    changed = {"caverns_of_time/nozdormu.py"}
    assert _code(changed, "Fix\n\n[skip-docs]\n") == 0
    assert _code(changed, "Fix\n\n  [docs-ok]  \n") == 0
    assert _code(changed, "Fix, see [skip-docs] in the guide\n") == 1


def test_exempt_files_are_ignored_but_other_areas_still_fail() -> None:
    changed = {"caverns_of_time/a.py", "dev_tools/b.py"}
    assert _code(changed, exempt=frozenset(changed)) == 0
    code, lines = ds.evaluate(changed, "", exempt=frozenset({"caverns_of_time/a.py"}))
    assert code == 1 and "[dev_tools]" in "\n".join(lines) and "[caverns_of_time]" not in "\n".join(lines)


def test_token_exempt_files_only_when_every_commit_carries_the_token() -> None:
    commits = [("A\n\n[skip-docs]", {"a.py", "b.py"}), ("B", {"b.py"})]
    assert ds.token_exempt_files(commits) == frozenset({"a.py"})


def test_dependabot_is_exempt_in_workflows_only() -> None:
    assert _code({".github/workflows/ci-tests.yml"}, actor=ds.DEPENDABOT_ACTOR) == 0
    assert _code({".github/workflows/ci-tests.yml"}, actor="someone") == 1
    assert _code({".github/workflows/ci-tests.yml", "dev_tools/x.py"}, actor=ds.DEPENDABOT_ACTOR) == 1


def test_parse_log_handles_blank_lines_and_spaces() -> None:
    raw = "\x00h1\x00Subject\n\nbody\n\x00\n\na b.py\nc.py\n\x00h2\x00Other\n\x00\nd.py\n"
    assert ds.parse_log(raw) == [("Subject\n\nbody\n", {"a b.py", "c.py"}), ("Other\n", {"d.py"})]


def test_git_failure_raises_git_error_and_main_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(ds.GitError):
        ds.git("rev-parse", "--verify", "no-such-ref-caverns")
    assert ds.main(["--base", "no-such-ref-caverns"]) == 2
    assert "docs-sync" in capsys.readouterr().out


def test_every_area_doc_exists() -> None:
    for _label, _prefix, doc in ds.AREAS:
        assert (ds.REPO_ROOT / doc).is_file(), doc
