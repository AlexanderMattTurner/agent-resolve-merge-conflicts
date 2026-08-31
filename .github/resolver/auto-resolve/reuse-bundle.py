#!/usr/bin/env python3
"""Reuse a prior run's still-valid resolution instead of re-buying it.

PROBLEM CLASS — a verified resolution held back at land time (a merge-queue
hold, a crashed land job, a cancel after the upload) is re-bought from scratch
by the next scan, even though nothing invalidated it. The artifact a run
uploads records the head it resolved in `parents.json`; while the PR head has
not moved, that bundle IS the merge a new run would pay the ladder to rebuild.

One name-filtered artifact listing names the newest bundle for this PR. When
its producing run passes the pins below and its recorded head parent is the
branch's current head, this step fills BUNDLE_DIR and answers `hit=true`, and
`land` pushes that bundle with no new spend. A base advance does not disqualify
one — `land` never compares the base ref.

A REFUSED run leaves no `merge.bundle`, only the paths it resolved before its
window ran out. That artifact answers `hit=false` and `salvage=<dir>`, so the
run ahead installs those paths and buys only the remainder — which is what lets
a conflict set larger than one window ever finish.

Every failure and every mismatch answers `hit=false` and resolves normally.

Env: GH_TOKEN, REPO, PR, HEAD_SHA, BUNDLE_DIR, GITHUB_OUTPUT, GITHUB_REF_NAME.
Optional: SALVAGE_DIR, where a carried partial resolution lands.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

JsonObject = dict[str, Any]

WORKFLOW_FILE = "auto-resolve-conflicts.yaml"

REQUIRED_ENV = (
    "REPO",
    "PR",
    "HEAD_SHA",
    "BUNDLE_DIR",
    "GITHUB_OUTPUT",
    # The branch this job runs on, which the producer pin below compares an
    # artifact's own run against. Actions sets it for every step.
    "GITHUB_REF_NAME",
)


def gh_api_bytes(path: str) -> bytes:
    """One `gh api` read, raw. Raises on nonzero exit — `main` owns the recovery."""
    done = subprocess.run(["gh", "api", path], stdout=subprocess.PIPE, check=True)
    return done.stdout


def gh_api_json(path: str) -> object:
    return json.loads(gh_api_bytes(path))


def object_of(answer: object) -> JsonObject:
    """The JSON object an API read answered, empty for any other shape. Every
    read here decides only whether to reuse, so a scalar or a list carries none
    of the wanted fields and falls through exactly as a missing field does."""
    return answer if isinstance(answer, dict) else {}


def rows_of(answer: object, key: str) -> list[JsonObject]:
    """The list-of-objects field an Actions listing carries under `key` —
    empty for any shape the API did not actually answer with."""
    rows = object_of(answer).get(key)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def newest_bundle_artifact(repo: str, pr: str) -> JsonObject | None:
    """The repo's newest `auto-resolve-merge-{pr}` artifact, or None when the
    listing holds none. Newest-only is the policy: an older artifact resolved
    an older head, so it is stale evidence the head check refuses anyway, and
    walking back to it spends listing calls to reach that same refusal."""
    name = f"auto-resolve-merge-{pr}"
    query = urlencode({"name": name, "per_page": "1"})
    rows = rows_of(gh_api_json(f"repos/{repo}/actions/artifacts?{query}"), "artifacts")
    if not rows:
        print(f"no prior '{name}' artifact — a normal resolve follows.")
        return None
    print(f"newest prior bundle: artifact {rows[0]['id']}")
    return rows[0]


def workflow_id(repo: str) -> object:
    """This workflow file's numeric id, which is how a RUN names the workflow it
    belongs to. A lookup that answers no id raises, so the pin can never pass by
    comparing two absences — `main`'s catch answers no hit."""
    return object_of(gh_api_json(f"repos/{repo}/actions/workflows/{WORKFLOW_FILE}"))[
        "id"
    ]


def producing_workflow(repo: str, run_id: object) -> object:
    """The workflow id of the run that uploaded the artifact, or None.

    PROBLEM CLASS — a pin read off a field the API does not serve refuses
    everything and looks like a policy. `/actions/artifacts` nests only `id`,
    `repository_id`, `head_repository_id`, `head_branch` and `head_sha` under
    `workflow_run`; a pin that reads a workflow id there names no workflow and
    refuses every artifact. The run's OWN record carries it, at one extra read
    per resolve. A row with no run id reads `.../runs/None`, which raises and
    reaches `main`'s catch — the malformed row is named rather than dressed as
    a producer mismatch.
    """
    return object_of(gh_api_json(f"repos/{repo}/actions/runs/{run_id}")).get(
        "workflow_id"
    )


def produced_here(repo: str, artifact: JsonObject, ref_name: str) -> bool:
    """Whether this workflow file, running on `ref_name`, uploaded `artifact`.

    INVARIANT — this refusal is what pins the producer. The reconciler
    dispatches this workflow only on the base branch, so a bundle minted by a
    rewritten copy of the workflow on a same-repo topic branch fails the branch
    read, and a bundle from another workflow fails the id read.
    """
    run = object_of(artifact.get("workflow_run"))
    if run.get("head_branch") != ref_name:
        print(
            f"the newest bundle came from branch {run.get('head_branch')!r}, not "
            f"{ref_name!r} — a normal resolve follows."
        )
        return False
    produced_by = producing_workflow(repo, run.get("id"))
    if produced_by != workflow_id(repo):
        print(
            f"the newest bundle came from workflow {produced_by}, not "
            f"{WORKFLOW_FILE} — a normal resolve follows."
        )
        return False
    return True


def salvage_dir() -> Path:
    """Where a carried partial resolution lands, for the step that installs it.

    Under the runner's temp by default, which survives the PR-head checkout that
    replaces the workspace between this step and that one."""
    named = os.environ.get("SALVAGE_DIR")
    if named:
        return Path(named)
    return Path(os.environ.get("RUNNER_TEMP", "/tmp"), "auto-resolve-salvage")  # noqa: S108


def take_salvage(extracted: Path, head_sha: str, salvage_dir: Path) -> bool:
    """Copy a refused run's partial resolution out, when it resolved THIS head.

    Answers whether the carry is available, never whether the bundle is
    reusable: `land` can push nothing from a salvage, so the run ahead still
    resolves — with the carried paths already merged and out of its way."""
    manifest = extracted / "salvage.json"
    if not manifest.is_file() or not (extracted / "salvage.patch").is_file():
        return False
    document = json.loads(manifest.read_text(encoding="utf-8"))
    recorded = document.get("head") if isinstance(document, dict) else None
    if recorded != head_sha:
        print(
            f"the prior salvage resolved head {recorded}; the branch is now at "
            f"{head_sha} — this run resolves the whole conflict."
        )
        return False
    salvage_dir.mkdir(parents=True, exist_ok=True)
    for name in ("salvage.patch", "salvage.json"):
        shutil.copyfile(extracted / name, salvage_dir / name)
    print(
        f"carrying round {document.get('round')}'s partial resolution of this "
        f"head: {len(document.get('paths') or [])} path(s) this run does not "
        "buy again."
    )
    return True


def fetch_and_verify(
    repo: str, artifact_id: int, head_sha: str, bundle_dir: Path
) -> tuple[bool, bool]:
    """Download the artifact; answer (bundle reusable, salvage carried).

    `bundle_dir` is filled only for the first, from a complete bundle whose
    recorded head parent is `head_sha`."""
    with tempfile.TemporaryDirectory() as scratch:
        zip_path = Path(scratch) / "artifact.zip"
        zip_path.write_bytes(
            gh_api_bytes(f"repos/{repo}/actions/artifacts/{artifact_id}/zip")
        )
        extracted = Path(scratch) / "contents"
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(extracted)
        # Neither a salvage-only artifact nor a pre-reuse one is a resolution
        # `land` could push. The salvage inside the first still carries forward.
        if (
            not (extracted / "merge.bundle").is_file()
            or not (extracted / "parents.json").is_file()
        ):
            print(
                "the prior artifact lacks merge.bundle or parents.json (a "
                "salvage-only or pre-reuse upload) — a normal resolve follows."
            )
            return False, take_salvage(extracted, head_sha, salvage_dir())
        parents = json.loads((extracted / "parents.json").read_text(encoding="utf-8"))
        recorded = parents.get("head") if isinstance(parents, dict) else None
        if recorded != head_sha:
            print(
                f"the prior bundle resolved head {recorded}; the branch is now "
                f"at {head_sha} — a normal resolve follows."
            )
            return False, False
        # A bundle whose producer ran the review carries `self-reviewed`; one
        # whose producer had the review on and could not run it carries
        # `unverified`. Either records what happened, so re-resolving buys the
        # caller nothing a rerun would answer differently. A bundle with NEITHER
        # was produced while the review was off, and this caller has it on.
        if os.environ.get("AUTO_RESOLVE_SELF_REVIEW") == "true" and not (
            (extracted / "self-reviewed").is_file()
            or (extracted / "unverified").is_file()
        ):
            print(
                "this caller runs the pre-push self-review, and the prior "
                "bundle records no such read — a normal resolve follows."
            )
            return False, take_salvage(extracted, head_sha, salvage_dir())
        shutil.copytree(extracted, bundle_dir, dirs_exist_ok=True)
    print(
        f"reusing the prior resolution: it resolved this exact head {head_sha}, "
        "so `land` verifies and pushes it with no new model spend."
    )
    return True, False


def reusable(repo: str) -> tuple[bool, bool]:
    """What a prior artifact holds for the current head: a reusable resolution,
    a partial one to carry, or neither."""
    artifact = newest_bundle_artifact(repo, os.environ["PR"])
    if artifact is None:
        return False, False
    if artifact.get("expired"):
        # GitHub keeps the row past the retention window with no bytes behind
        # it, so downloading one only buys a 404.
        print("the newest bundle has expired — a normal resolve follows.")
        return False, False
    if not produced_here(repo, artifact, os.environ["GITHUB_REF_NAME"]):
        return False, False
    return fetch_and_verify(
        repo,
        artifact["id"],
        os.environ["HEAD_SHA"],
        Path(os.environ["BUNDLE_DIR"]),
    )


def emit(hit: bool, salvage: bool) -> None:
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
        out.write(f"hit={'true' if hit else 'false'}\n")
        out.write(f"salvage={salvage_dir() if salvage else ''}\n")


def main() -> None:
    for name in REQUIRED_ENV:
        if not os.environ.get(name):
            print(f"::error::{name} required", file=sys.stderr)
            raise SystemExit(1)
    try:
        hit, salvage = reusable(os.environ["REPO"])
    except Exception as err:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # Forgives ANY probe failure — an absent `gh`, an API blip, a truncated
        # zip, a row missing `id`, non-UTF-8 bytes in parents.json. The probe
        # only saves money and `land` re-verifies whatever it hands on, so a
        # failure costs one re-buy; escaping would instead fail the step and
        # SKIP the paid resolve, which every later step gates on `hit != true`.
        print(f"could not read the prior artifact ({err}) — a normal resolve follows.")
        hit, salvage = False, False
    emit(hit, salvage)


if __name__ == "__main__":
    main()
