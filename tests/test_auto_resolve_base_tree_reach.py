"""No resolve step reads the caller's base tree on a run that never cloned it.

PROBLEM CLASS — a step reads a directory an earlier, CONDITIONAL step created.
The `base` step clones the calling repository's default branch, and it runs only
for a dispatch that selected a pull request and found no reusable bundle. On
every other path `steps.base.outputs.dir` is the empty string, so a reader that
lost its own gate resolves `${{ steps.base.outputs.dir }}/<file>` to `/<file>` —
an absolute path outside any checkout — instead of failing loud.

Each gate is one `if:`, so a later edit drops one silently. This reads the
shipped workflow with a real YAML parser and walks every reader's gate up to the
clone's own two conditions.

# covers: .github/workflows/auto-resolve.yaml
"""

import yaml

from tests._helpers import REPO_ROOT

REUSABLE = REPO_ROOT / ".github" / "workflows" / "auto-resolve.yaml"

SELECTION = "steps.selected.outputs.selected == 'true'"
NO_REUSE_HIT = "steps.reuse.outputs.hit != 'true'"

# A reader may gate on either of these. `prepare` reaches the clone's conditions
# through `mark`, and `mark` carries both of them itself.
READER_GATES = ("steps.mark.", "steps.prepare.")

BASE_OUTPUT = "steps.base.outputs."


def _resolve_steps() -> list[dict]:
    doc = yaml.safe_load(REUSABLE.read_text(encoding="utf-8"))
    steps = doc["jobs"]["resolve"]["steps"]
    assert steps, "read no step from the resolve job — every case below would pass over nothing"
    return steps


def _readers(steps: list[dict]) -> list[tuple[int, dict]]:
    """Every step but the clone itself whose body names the cloned tree."""
    return [
        (index, step)
        for index, step in enumerate(steps)
        if step.get("id") != "base" and BASE_OUTPUT in yaml.safe_dump(step)
    ]


def _step(steps: list[dict], step_id: str) -> dict:
    found = [step for step in steps if step.get("id") == step_id]
    assert found, f"the resolve job has no step with id {step_id!r}"
    return found[0]


def test_the_clone_runs_only_for_a_dispatch_that_still_has_a_merge_to_make() -> None:
    condition = str(_step(_resolve_steps(), "base").get("if", ""))
    for required in (SELECTION, NO_REUSE_HIT):
        assert required in condition, (
            f"the base clone no longer carries {required!r}; it is paid again on a "
            "dispatch that resolves nothing"
        )


def test_every_reader_of_the_base_tree_is_gated_behind_the_clone() -> None:
    steps = _resolve_steps()
    base_index = next(i for i, step in enumerate(steps) if step.get("id") == "base")
    readers = _readers(steps)
    assert len(readers) >= 5, (
        f"only {len(readers)} step(s) read {BASE_OUTPUT}; this case has stopped covering "
        "the readers it was written for"
    )
    for index, step in readers:
        name = step.get("name", step.get("id", f"step {index}"))
        assert index > base_index, f"{name!r} reads the base tree before the clone creates it"
        condition = str(step.get("if", ""))
        assert any(gate in condition for gate in READER_GATES), (
            f"{name!r} reads the base tree without gating on {' or '.join(READER_GATES)}, "
            "so it runs on a dispatch that cloned nothing and reads an absolute path"
        )


def test_the_reader_gates_reach_the_clone_s_own_conditions() -> None:
    steps = _resolve_steps()
    mark = str(_step(steps, "mark").get("if", ""))
    for required in (SELECTION, NO_REUSE_HIT):
        assert required in mark, (
            f"`mark` no longer carries {required!r}, so a reader gated on it can run "
            "where the clone did not"
        )
    prepare = str(_step(steps, "prepare").get("if", ""))
    assert "steps.mark." in prepare, (
        "`prepare` no longer gates on `mark`, so a reader gated on its outputs no "
        "longer reaches the clone's conditions"
    )


def test_a_reused_bundle_needs_no_base_tree() -> None:
    reuse = _step(_resolve_steps(), "reuse")
    assert BASE_OUTPUT not in yaml.safe_dump(reuse), (
        "`reuse` reads the base tree again, so the clone must run before it and a "
        "reuse hit pays for a tree it never reads"
    )
