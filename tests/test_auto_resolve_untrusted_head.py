"""The resolve job may run NO code a fork head supplies.

PROBLEM CLASS — a job holding secrets that executes code from an untrusted pull
request. The resolve job holds every model credential, and a fork's author is
anyone; a same-repo branch already required push access. The job checks the head
out either way, so what keeps a fork safe is that the four steps able to execute
that checkout are gated on the head living in the calling repository:

  * the caller's local composite action, which the fork's own tree would supply;
  * the caller's pre-pass command, a script the head's manifest defines;
  * the head's pre-commit hooks, and the toolchain installed to run them;
  * the generated-region pass, which runs the generator a region NAMES;
  * the merged tree's own pnpm lockfile, which names what an install fetches.

Each gate is one `if:` or one `env:` value, so a later edit drops one silently.
This reads the shipped workflow with a real YAML parser and fails when a named
step stops carrying its gate.

# covers: .github/workflows/auto-resolve.yaml
"""

import pytest
import yaml

from tests._helpers import REPO_ROOT

REUSABLE = REPO_ROOT / ".github" / "workflows" / "auto-resolve.yaml"

SAME_REPO = "steps.selected.outputs.head_repo == github.repository"

# The steps whose BODY executes head-supplied code, by name. Each must refuse to
# run at all on a fork head.
GATED_STEPS = (
    "Set up Node + install deps",
    "Install pre-commit",
    "Install the pinned hook toolchain (binaries + Python packages)",
)

# The steps that RUN on a fork head, each with the env keys that must go empty
# there — every one names something in the merged tree the step would execute.
GATED_ENV = {
    "Merge base, deterministic pre-pass, and protected-path flag": (
        "AUTO_RESOLVE_PRE_PASS",
        "AUTO_RESOLVE_RESOLVER_MJS",
        "AUTO_RESOLVE_MARKED_REGIONS",
    ),
    "Verify, self-review, and bundle the merge for the land job": (
        "AUTO_RESOLVE_RESOLVER_MJS",
        "AUTO_RESOLVE_MARKED_REGIONS",
    ),
}


def _resolve_steps() -> dict[str, dict]:
    doc = yaml.safe_load(REUSABLE.read_text(encoding="utf-8"))
    steps = {
        step["name"]: step for step in doc["jobs"]["resolve"]["steps"] if "name" in step
    }
    assert steps, (
        "read no named step from the resolve job — every case below would pass over nothing"
    )
    return steps


def test_every_head_executing_step_refuses_a_fork_head() -> None:
    steps = _resolve_steps()
    for name in GATED_STEPS:
        assert name in steps, f"{name} is gone; the gate it carried went with it"
        assert SAME_REPO in str(steps[name].get("if", "")), (
            f"{name} runs code the head supplies and no longer refuses a fork head"
        )


def test_every_head_reading_knob_goes_empty_on_a_fork_head() -> None:
    steps = _resolve_steps()
    for name, keys in GATED_ENV.items():
        assert name in steps, f"{name} is gone; the gates it carried went with it"
        env = steps[name].get("env") or {}
        for key in keys:
            assert SAME_REPO in str(env.get(key, "")), (
                f"{name} passes {key} unconditionally, so a fork head's own "
                "generators would run in the job holding every credential"
            )


@pytest.mark.parametrize(
    "step",
    [
        "Merge base, deterministic pre-pass, and protected-path flag",
        "Verify, self-review, and bundle the merge for the land job",
    ],
)
def test_the_untrusted_head_flag_reaches_both_python_steps(step: str) -> None:
    """The other half. bundle.py skips both hook passes under this flag and
    prepare.sh skips the lockfile install, and the resolve job installs neither
    toolchain for such a run — so a flag that stopped being set would make one
    step lint what it cannot lint and the other fetch what the fork named."""
    env = _resolve_steps()[step]["env"]
    assert env["AUTO_RESOLVE_UNTRUSTED_HEAD"] == (
        "${{ steps.selected.outputs.head_repo != github.repository }}"
    )


def test_both_jobs_check_out_the_head_s_own_repository() -> None:
    """A fork's resolution has to be pushed to the fork, and `origin` is what
    land.sh pushes to. A checkout that named the base repository would resolve
    the wrong branch, or none."""
    doc = yaml.safe_load(REUSABLE.read_text(encoding="utf-8"))
    resolve = {s.get("name"): s for s in doc["jobs"]["resolve"]["steps"]}
    land = {s.get("name"): s for s in doc["jobs"]["land"]["steps"]}
    assert resolve["Checkout PR head"]["with"]["repository"] == (
        "${{ steps.selected.outputs.head_repo }}"
    )
    assert land["Checkout the PR head"]["with"]["repository"] == (
        "${{ needs.resolve.outputs.head_repo }}"
    )
