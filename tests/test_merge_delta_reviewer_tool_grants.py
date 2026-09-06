"""Every file the merge-delta reviewer is pointed at is one it may open.

covers: .github/workflows/merge-delta-review.yaml

`--add-dir` puts a directory inside the session's workspace. It grants no tool
permission on its own, so a path named in the prompt with no matching `Read`
rule in `--allowedTools` is refused at every open. The reviewer then reports the
head unreviewed and the caller posts a blocking finding that no push can clear.

The check is generic: it reads the paths the step's own `prompt` and
`untrusted_input` name, and asserts a rule covers each. A later prompt that
names a new input file is covered without naming that file here.
"""

import pathlib
import re
import subprocess

import pytest
import yaml

REPO_ROOT = pathlib.Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
)
WORKFLOW = REPO_ROOT / ".github/workflows/merge-delta-review.yaml"

# The two expressions the reviewer step spells its paths with. Any value works:
# the test asserts a rule covers the path, not what the runner expands it to.
SUBSTITUTIONS = {
    "${{ runner.temp }}": "/runner/_temp",
    "${{ steps.resolver.outputs.prompts }}": "/runner/_temp/resolver/.github/prompts",
}
# A path the step hands the model: absolute, and a file rather than a directory.
PATH_IN_PROSE = re.compile(r"/[\w./-]+\.(?:txt|md|json)")


def _expand(text: str) -> str:
    for expression, value in SUBSTITUTIONS.items():
        text = text.replace(expression, value)
    return text


def _reviewer_step() -> dict:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["review"]["steps"]
    return next(s for s in steps if "claude_args" in (s.get("with") or {}))


def _rules(tool: str) -> list[str]:
    """The paths `--allowedTools` grants `tool`, expanded."""
    args = _expand(_reviewer_step()["with"]["claude_args"])
    return re.findall(rf"{tool}\(([^)]*)\)", args)


def _covers(rule: str, path: str) -> bool:
    if rule.endswith("/**"):
        return path.startswith(rule[: -len("**")])
    return rule == path


def _named_paths(key: str) -> list[str]:
    return sorted(set(PATH_IN_PROSE.findall(_expand(_reviewer_step()["with"][key]))))


@pytest.mark.parametrize("key", ["prompt", "untrusted_input"])
def test_every_input_path_the_step_names_has_a_read_grant(key: str) -> None:
    """The defect this pins: the merge deltas were named with no rule covering
    them, so the reviewer read nothing and called the head unreviewed."""
    rules = _rules("Read")
    named = _named_paths(key)

    assert named, f"the step's {key} names no path, so this check pins nothing"
    for path in named:
        assert any(_covers(rule, path) for rule in rules), (
            f"{key} points the reviewer at {path}, which no Read rule covers: {rules}"
        )


def test_the_review_file_is_the_only_writable_path() -> None:
    """The step's own comment promises exactly this, and a whole-tool `Edit`
    grant or a directory glob would silently widen it."""
    assert _rules("Edit") == ["/runner/_temp/pr-input/merge-review.md"]


def test_no_rule_carries_a_doubled_leading_slash() -> None:
    """`/${{ runner.temp }}` reads as one path and expands to another, so a rule
    written that way matches nothing the runner ever opens."""
    for tool in ("Read", "Edit"):
        for rule in _rules(tool):
            assert not rule.startswith("//"), rule
