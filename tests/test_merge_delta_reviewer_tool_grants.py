"""Every file a Claude reviewer step is pointed at is one it may open.

covers: .github/workflows/claude-review.yaml, .github/workflows/merge-delta-review.yaml

`--add-dir` puts a directory inside the session's workspace. It grants no tool
permission on its own, so a path named in the prompt with no matching `Read`
rule in `--allowedTools` is refused at every open. The reviewer then reports the
head unreviewed and the caller posts a blocking finding that no push can clear.

The check is generic twice over. It finds every step that passes `claude_args`,
in every workflow, so a third reviewer is covered the day it is written. And it
reads the paths from that step's own `prompt` and `untrusted_input`, so a later
prompt naming a new input file is covered without being named here.

A `${{ }}` expression becomes a synthetic path segment rather than a guessed
value: what matters is that the rule and the prompt spell the same expression,
not what the runner expands it to.
"""

import re

import pytest
import yaml

from tests._helpers import REPO_ROOT

WORKFLOWS = REPO_ROOT / ".github/workflows"
# A path the step hands the model: absolute, and a file rather than a directory.
# The lookbehind keeps a RELATIVE path out: `.github/prompts/x.md` would otherwise
# match from its second slash and read as an absolute path nothing grants.
PATH_IN_PROSE = re.compile(r"(?<![\w./-])/[\w./-]+\.(?:txt|md|json)")
EXPRESSION = re.compile(r"\$\{\{(?P<expr>[^}]*)\}\}")


def _expand(text: str) -> str:
    """Each `${{ … }}` becomes one absolute segment naming the expression."""
    return EXPRESSION.sub(
        lambda m: "/expr/" + re.sub(r"[^\w]+", "-", m.group(1).strip()), text
    )


def _reviewer_steps() -> list[tuple[str, dict]]:
    found = []
    for path in sorted(WORKFLOWS.glob("*.yaml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_id, job in (workflow.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if "claude_args" in (step.get("with") or {}):
                    found.append((f"{path.name}:{job_id}", step["with"]))
    return found


STEPS = _reviewer_steps()
IDS = [name for name, _ in STEPS]


def _rules(step: dict, tool: str) -> list[str]:
    """The paths `--allowedTools` grants `tool`, expanded."""
    return re.findall(rf"{tool}\(([^)]*)\)", _expand(step["claude_args"]))


def _covers(rule: str, path: str) -> bool:
    if rule.endswith("/**"):
        return path.startswith(rule[: -len("**")])
    return rule == path


def _named_paths(step: dict) -> list[str]:
    prose = " ".join(step.get(k, "") for k in ("prompt", "untrusted_input"))
    return sorted(set(PATH_IN_PROSE.findall(_expand(prose))))


def test_at_least_one_step_scopes_its_reads_by_path() -> None:
    """A rename of `claude_args`, or a sweep to whole-tool grants, would leave
    every test below skipping and reporting green."""
    assert [name for name, step in STEPS if _rules(step, "Read") and _named_paths(step)]


@pytest.mark.parametrize(("name", "step"), STEPS, ids=IDS)
def test_every_input_path_the_step_names_has_a_read_grant(
    name: str, step: dict
) -> None:
    """The defect this pins: the merge deltas were named with no rule covering
    them, so the reviewer read nothing and called the head unreviewed.

    A step with no `Read(<path>)` rule at all scopes Read some other way — a bare
    `Read` in the list is a whole-tool grant — so it has no per-path claim to check.
    """
    rules = _rules(step, "Read")
    if not rules:
        pytest.skip(f"{name} does not scope Read by path")
    named = _named_paths(step)

    for path in named:
        assert any(_covers(rule, path) for rule in rules), (
            f"{name} points the reviewer at {path}, which no Read rule covers: {rules}"
        )


@pytest.mark.parametrize(("name", "step"), STEPS, ids=IDS)
def test_the_write_grant_names_files_and_never_a_directory(
    name: str, step: dict
) -> None:
    """A reviewer writes one file. A directory glob lets it write over every
    input it was handed, including the ones a caller staged out of its reach."""
    for rule in _rules(step, "Edit"):
        assert "*" not in rule, f"{name} grants Edit on a glob: {rule}"


@pytest.mark.parametrize(("name", "step"), STEPS, ids=IDS)
def test_no_rule_carries_a_doubled_leading_slash(name: str, step: dict) -> None:
    """`/${{ runner.temp }}` reads as one path and expands to another, so a rule
    written that way matches nothing the runner ever opens."""
    for tool in ("Read", "Edit"):
        for rule in _rules(step, tool):
            assert not rule.startswith("//"), f"{name}: {rule}"
