""".github/resolver/auto-resolve/reuse-bundle.py — C7's free retry.

Drives the real script IN THIS INTERPRETER, against a localhost GitHub
(`FakeActionsArtifacts`) that the real `gh` binary talks to: the name-filtered
artifact listing, the workflow-file lookup the producer pin compares against,
and the artifact-zip download with its real 302-to-blob redirect — so a wrong
API path, a misread field name, or a client that does not follow the redirect
fails loudly instead of passing through a stub's agreeing answer. In-process is
what coverage can trace; the same script driven as a subprocess reports 0%
however thoroughly it is exercised.

The fixture rows carry only the fields the script reads (artifact `id`, `name`,
`expired`, and its run's `id`, `head_branch`, `head_sha`), the smallest
redaction of the real `/actions/artifacts` reply — which carries NO
`workflow_id` under `workflow_run`, so the producer pin reads that from the
run's own record.
"""

# covers: .github/resolver/auto-resolve/reuse-bundle.py

import io
import json
import zipfile
from pathlib import Path

import pytest

from tests._fake_github import FakeActionsArtifacts
from tests._resolver_helpers import load_script

reuse = load_script(".github/resolver/auto-resolve/reuse-bundle.py")

PR = "7"
BUNDLE_NAME = f"auto-resolve-merge-{PR}"

CURRENT_HEAD = "c0ffee" * 6 + "beef"
MOVED_HEAD = "0ddba1" * 6 + "dead"


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def _bundle_zip(head: str) -> bytes:
    """An artifact's bytes as the resolve job uploads them: the bundle, the head
    claim, and the sidecar files beside them."""
    return _zip(
        {
            "merge.bundle": b"BUNDLE-BYTES",
            "parents.json": json.dumps({"head": head, "base": "b" * 40}).encode()
            + b"\n",
            "rung": b"3\n",
        }
    )


def _seed(server: FakeActionsArtifacts, artifact_id: int, zip_bytes: bytes, **row):
    """One reusable-looking artifact for this PR, plus the bytes behind it."""
    server.zips[artifact_id] = zip_bytes
    return server.add_artifact(artifact_id, BUNDLE_NAME, **row)


def _run_reuse(
    server: FakeActionsArtifacts,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    head_sha: str = CURRENT_HEAD,
    ref_name: str | None = None,
    drop: str = "",
) -> tuple[Path, dict[str, str]]:
    """Call the step's own `main()` with the job's environment, and read back
    what it left in BUNDLE_DIR and GITHUB_OUTPUT. `drop` unsets one variable,
    which is how the workflow looks when a step forgets to pass it."""
    bundle_dir = tmp_path / "bundle-dir"
    github_output = tmp_path / "github-output"
    github_output.touch()
    env = {
        **server.env,
        "REPO": server.repo,
        "PR": PR,
        "HEAD_SHA": head_sha,
        "BUNDLE_DIR": str(bundle_dir),
        "GITHUB_OUTPUT": str(github_output),
        "GITHUB_REF_NAME": server.branch if ref_name is None else ref_name,
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(drop, raising=False)
    reuse.main()
    outputs = dict(
        line.split("=", 1)
        for line in github_output.read_text(encoding="utf-8").splitlines()
    )
    return bundle_dir, outputs


# --- the two shape readers, whose forgiveness decides only whether to reuse ---


@pytest.mark.parametrize(
    ("answer", "expected"),
    [({"a": 1}, {"a": 1}), ([], {}), ("text", {}), (None, {})],
    ids=["object", "list", "scalar", "null"],
)
def test_only_a_json_object_is_read_for_fields(answer, expected):
    assert reuse.object_of(answer) == expected


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"artifacts": [{"id": 1}, {"id": 2}]}, [{"id": 1}, {"id": 2}]),
        ({"artifacts": [{"id": 1}, "junk"]}, [{"id": 1}]),
        ({"artifacts": "not a list"}, []),
        ({"message": "Not Found"}, []),
        ([], []),
    ],
    ids=["rows", "mixed", "not_a_list", "absent", "not_an_object"],
)
def test_the_listing_rows_are_the_objects_the_api_actually_answered(answer, expected):
    assert reuse.rows_of(answer, "artifacts") == expected


# --- the probe, driven end to end against a real gh ---------------------------


def test_a_bundle_recording_the_current_head_is_reused(tmp_path, monkeypatch, capsys):
    """The hit path: another PR's newer artifact is filtered out by name, this
    PR's newest bundle records this exact head, and its whole contents land in
    BUNDLE_DIR for the upload step to re-publish."""
    with FakeActionsArtifacts(tmp_path) as server:
        _seed(server, 55, _bundle_zip(CURRENT_HEAD))
        server.add_artifact(90, "auto-resolve-merge-8")
        bundle_dir, outputs = _run_reuse(server, tmp_path, monkeypatch)
    assert outputs == {"hit": "true", "salvage": ""}
    assert (bundle_dir / "merge.bundle").read_bytes() == b"BUNDLE-BYTES"
    assert (bundle_dir / "rung").read_text(encoding="utf-8") == "3\n"
    assert (
        json.loads((bundle_dir / "parents.json").read_text(encoding="utf-8"))["head"]
        == CURRENT_HEAD
    )
    assert "no new model spend" in capsys.readouterr().out


def test_an_opted_in_caller_refuses_a_bundle_with_no_recorded_review(
    tmp_path, monkeypatch, capsys
):
    """A caller that opts in to the self-review must never reuse a resolution
    produced while the review was off: only a bundle carrying the
    `self-reviewed` marker bundle.py writes may hit."""
    monkeypatch.setenv("AUTO_RESOLVE_SELF_REVIEW", "true")
    with FakeActionsArtifacts(tmp_path) as server:
        _seed(server, 55, _bundle_zip(CURRENT_HEAD))
        bundle_dir, outputs = _run_reuse(server, tmp_path, monkeypatch)
    assert outputs == {"hit": "false", "salvage": ""}
    assert not bundle_dir.exists()
    assert "records no such read" in capsys.readouterr().out


def test_a_caller_with_no_credential_reuses_the_bundle_it_could_not_review(
    tmp_path, monkeypatch
):
    """The review is on by DEFAULT now, so a caller that configured no credential
    reaches it on every resolve and bundle.py marks the result `unverified`.
    Refusing that bundle would never terminate: the next run for this head takes
    the same branch and produces the same bundle, so the resolve is re-bought
    forever. `unverified` records what happened, so the reuse stands."""
    monkeypatch.setenv("AUTO_RESOLVE_SELF_REVIEW", "true")
    files = {
        "merge.bundle": b"BUNDLE-BYTES",
        "parents.json": json.dumps({"head": CURRENT_HEAD, "base": "b" * 40}).encode()
        + b"\n",
        "unverified": b"the pre-push merge-delta reviewer produced no verdict\n",
    }
    with FakeActionsArtifacts(tmp_path) as server:
        _seed(server, 55, _zip(files))
        bundle_dir, outputs = _run_reuse(server, tmp_path, monkeypatch)
    assert outputs == {"hit": "true", "salvage": ""}
    assert (bundle_dir / "unverified").exists()


def test_an_opted_in_caller_reuses_a_bundle_whose_review_is_recorded(
    tmp_path, monkeypatch
):
    """The marker bundle.py writes is what admits a bundle past an opted-in
    caller, and the reused tree keeps it for the upload step to re-publish."""
    monkeypatch.setenv("AUTO_RESOLVE_SELF_REVIEW", "true")
    files = {
        "merge.bundle": b"BUNDLE-BYTES",
        "parents.json": json.dumps({"head": CURRENT_HEAD, "base": "b" * 40}).encode()
        + b"\n",
        "self-reviewed": b"read\n",
    }
    with FakeActionsArtifacts(tmp_path) as server:
        _seed(server, 55, _zip(files))
        bundle_dir, outputs = _run_reuse(server, tmp_path, monkeypatch)
    assert outputs == {"hit": "true", "salvage": ""}
    assert (bundle_dir / "self-reviewed").exists()


def test_a_head_that_moved_falls_through_to_a_normal_resolve(
    tmp_path, monkeypatch, capsys
):
    """A push to the PR made the recorded resolution stale — nothing may land
    in BUNDLE_DIR, or the upload step would ship the stale bundle to `land`."""
    with FakeActionsArtifacts(tmp_path) as server:
        _seed(server, 55, _bundle_zip(MOVED_HEAD))
        bundle_dir, outputs = _run_reuse(server, tmp_path, monkeypatch)
    assert outputs == {"hit": "false", "salvage": ""}
    assert not bundle_dir.exists()
    printed = capsys.readouterr().out
    assert MOVED_HEAD in printed and CURRENT_HEAD in printed


def test_only_the_newest_artifact_is_tried(tmp_path, monkeypatch):
    """Newest-only is the policy: an older artifact resolved an older head, so
    a walk back to it would reuse evidence a later push already invalidated —
    here the older bundle would match, and must never be downloaded."""
    with FakeActionsArtifacts(tmp_path) as server:
        _seed(server, 41, _bundle_zip(CURRENT_HEAD))
        _seed(server, 55, _bundle_zip(MOVED_HEAD))
        bundle_dir, outputs = _run_reuse(server, tmp_path, monkeypatch)
        downloads = [path for _, path in server.requests if path.endswith("/zip")]
    assert outputs == {"hit": "false", "salvage": ""}
    assert not bundle_dir.exists()
    assert not any("/artifacts/41/" in path for path in downloads), downloads


def test_a_salvage_only_artifact_is_never_reused(tmp_path, monkeypatch):
    """A handoff's artifact holds a partial patch for a human, not a resolution
    `land` could push — reusing it would upload a bundle-less artifact that
    lands nothing while the hit skips the resolve that would."""
    with FakeActionsArtifacts(tmp_path) as server:
        _seed(server, 55, _zip({"salvage.patch": b"diff --git a/x b/x\n"}))
        bundle_dir, outputs = _run_reuse(server, tmp_path, monkeypatch)
    assert outputs == {"hit": "false", "salvage": ""}
    assert not bundle_dir.exists()


# --- the carry: what a REFUSED run leaves for the run behind it ---------------


def _refused(tmp_path: Path, head: str, name: str = "extracted") -> Path:
    """An extracted salvage-only artifact, as a refusing run uploads one."""
    extracted = tmp_path / name
    extracted.mkdir()
    (extracted / "salvage.patch").write_text("diff --git a/x b/x\n", encoding="utf-8")
    (extracted / "salvage.json").write_text(
        json.dumps({"head": head, "merge_base": "b" * 40, "paths": ["x"], "round": 1}),
        encoding="utf-8",
    )
    return extracted


def test_a_salvage_pinned_to_this_head_is_carried(tmp_path, capsys):
    """The whole convergence: what the last round resolved reaches the next run,
    so a conflict set larger than one window shrinks instead of repeating."""
    destination = tmp_path / "carry"
    assert reuse.take_salvage(
        _refused(tmp_path, CURRENT_HEAD), CURRENT_HEAD, destination
    )
    assert (
        (destination / "salvage.patch").read_text(encoding="utf-8").startswith("diff")
    )
    assert (
        json.loads((destination / "salvage.json").read_text(encoding="utf-8"))["round"]
        == 1
    )
    assert "1 path(s) this run does not buy again" in capsys.readouterr().out


def test_a_salvage_pinned_to_another_head_is_not_carried(tmp_path, capsys):
    """A push moved the branch, so those paths were resolved against a tree this
    run is not merging — installing them would put content in neither parent."""
    destination = tmp_path / "carry"
    assert not reuse.take_salvage(
        _refused(tmp_path, MOVED_HEAD), CURRENT_HEAD, destination
    )
    assert not destination.exists()
    assert MOVED_HEAD in capsys.readouterr().out


def test_an_artifact_with_no_salvage_manifest_is_not_carried(tmp_path):
    """A patch with no manifest pins nothing — neither the head it resolved nor
    the merge base it was cut from, which are what make it installable."""
    extracted = _refused(tmp_path, CURRENT_HEAD)
    (extracted / "salvage.json").unlink()
    assert not reuse.take_salvage(extracted, CURRENT_HEAD, tmp_path / "carry")


def test_an_artifact_with_no_head_claim_is_never_reused(tmp_path, monkeypatch):
    """A bundle from before parents.json existed carries no claim about the head
    it resolved, so nothing here can tell whether it is still the merge to push."""
    with FakeActionsArtifacts(tmp_path) as server:
        _seed(server, 55, _zip({"merge.bundle": b"BUNDLE-BYTES"}))
        bundle_dir, outputs = _run_reuse(server, tmp_path, monkeypatch)
    assert outputs == {"hit": "false", "salvage": ""}
    assert not bundle_dir.exists()


def test_a_head_claim_that_is_not_an_object_is_never_reused(tmp_path, monkeypatch):
    """Readable JSON of the wrong shape names no head, and "no head recorded"
    must never compare equal to the head this run would reuse against."""
    with FakeActionsArtifacts(tmp_path) as server:
        _seed(server, 55, _zip({"merge.bundle": b"B", "parents.json": b"[]"}))
        bundle_dir, outputs = _run_reuse(server, tmp_path, monkeypatch)
    assert outputs == {"hit": "false", "salvage": ""}
    assert not bundle_dir.exists()


def test_an_expired_artifact_is_not_reused(tmp_path, monkeypatch):
    """GitHub keeps the artifact ROW past its retention window with
    `expired: true` and no downloadable bytes — reading the row as a hit would
    404 the download on every reuse attempt."""
    with FakeActionsArtifacts(tmp_path) as server:
        server.add_artifact(55, BUNDLE_NAME, expired=True)
        bundle_dir, outputs = _run_reuse(server, tmp_path, monkeypatch)
    assert outputs == {"hit": "false", "salvage": ""}
    assert not bundle_dir.exists()


def test_no_prior_artifact_answers_no_hit(tmp_path, monkeypatch):
    with FakeActionsArtifacts(tmp_path) as server:
        bundle_dir, outputs = _run_reuse(server, tmp_path, monkeypatch)
    assert outputs == {"hit": "false", "salvage": ""}
    assert not bundle_dir.exists()


# --- the producer pin ---------------------------------------------------------


def test_an_artifact_produced_on_another_branch_is_refused(
    tmp_path, monkeypatch, capsys
):
    """The reconciler dispatches this workflow only on the base branch, so a
    matching artifact minted from a same-repo topic branch is a rewritten
    workflow's upload, not a prior run of this one."""
    with FakeActionsArtifacts(tmp_path) as server:
        _seed(server, 55, _bundle_zip(CURRENT_HEAD), head_branch="attacker-branch")
        bundle_dir, outputs = _run_reuse(server, tmp_path, monkeypatch)
    assert outputs == {"hit": "false", "salvage": ""}
    assert not bundle_dir.exists()
    assert "attacker-branch" in capsys.readouterr().out


def test_an_artifact_produced_by_another_workflow_is_refused(
    tmp_path, monkeypatch, capsys
):
    """Any workflow on the base branch can upload an artifact under this name;
    only this workflow file's own runs produce a bundle `land` should push."""
    with FakeActionsArtifacts(tmp_path) as server:
        _seed(server, 55, _bundle_zip(CURRENT_HEAD), workflow_id=999)
        bundle_dir, outputs = _run_reuse(server, tmp_path, monkeypatch)
    assert outputs == {"hit": "false", "salvage": ""}
    assert not bundle_dir.exists()
    assert "999" in capsys.readouterr().out


def test_a_run_that_names_no_workflow_is_refused(tmp_path, monkeypatch, capsys):
    """The pin refuses an absent workflow id rather than reading it as a match.

    The listing row carries none — the live `/actions/artifacts` reply has no
    `workflow_id` under `workflow_run` — so a pin reading it there compares two
    absences and reuses whatever any workflow uploaded under this name."""
    with FakeActionsArtifacts(tmp_path) as server:
        row = _seed(server, 55, _bundle_zip(CURRENT_HEAD))
        assert "workflow_id" not in row["workflow_run"]
        server.runs[row["workflow_run"]["id"]] = None
        bundle_dir, outputs = _run_reuse(server, tmp_path, monkeypatch)
    assert outputs == {"hit": "false", "salvage": ""}
    assert not bundle_dir.exists()
    assert "came from workflow None" in capsys.readouterr().out


def test_a_producing_run_that_cannot_be_read_is_refused(tmp_path, monkeypatch, capsys):
    """An unreadable run raises out of the pin, `main` forgives the probe, and
    the run names the read that failed instead of blaming the producer."""
    with FakeActionsArtifacts(tmp_path) as server:
        _seed(server, 55, _bundle_zip(CURRENT_HEAD))
        server.runs.clear()
        bundle_dir, outputs = _run_reuse(server, tmp_path, monkeypatch)
    assert outputs == {"hit": "false", "salvage": ""}
    assert not bundle_dir.exists()
    assert "could not read the prior artifact" in capsys.readouterr().out


# --- every failure answers hit=false, so the paid resolve still runs ----------


def test_an_api_failure_answers_no_hit_rather_than_failing_the_job(
    tmp_path, monkeypatch, capsys
):
    """The probe only saves money: a listing outage must fall through to the
    normal resolve (the pre-reuse behavior), never take the paid resolve down
    with it — and it must say so, not pass silently."""
    with FakeActionsArtifacts(tmp_path) as server:
        server.fail_listings = True
        bundle_dir, outputs = _run_reuse(server, tmp_path, monkeypatch)
    assert outputs == {"hit": "false", "salvage": ""}
    assert not bundle_dir.exists()
    assert "a normal resolve follows." in capsys.readouterr().out


def test_a_parents_json_that_is_not_utf8_answers_no_hit(tmp_path, monkeypatch, capsys):
    """A truncated or binary head claim raises a UnicodeDecodeError no listed
    exception type covers. Letting it escape fails the step, and GitHub then
    SKIPS every later step gated on `hit != 'true'` — killing the paid resolve
    this probe exists to protect."""
    with FakeActionsArtifacts(tmp_path) as server:
        _seed(
            server,
            55,
            _zip({"merge.bundle": b"B", "parents.json": b"\xff\xfe\x00head"}),
        )
        bundle_dir, outputs = _run_reuse(server, tmp_path, monkeypatch)
    assert outputs == {"hit": "false", "salvage": ""}
    assert not bundle_dir.exists()
    assert "could not read the prior artifact" in capsys.readouterr().out


def test_an_artifact_row_with_no_id_answers_no_hit(tmp_path, monkeypatch, capsys):
    """A row the API answered without the field this step indexes raises a
    KeyError, which the same catch has to forgive for the same reason."""
    with FakeActionsArtifacts(tmp_path) as server:
        _seed(server, 55, _bundle_zip(CURRENT_HEAD)).pop("id")
        bundle_dir, outputs = _run_reuse(server, tmp_path, monkeypatch)
    assert outputs == {"hit": "false", "salvage": ""}
    assert not bundle_dir.exists()
    assert "could not read the prior artifact" in capsys.readouterr().out


@pytest.mark.parametrize("missing", reuse.REQUIRED_ENV)
def test_a_missing_required_input_is_refused(tmp_path, monkeypatch, capsys, missing):
    """An unset input is a plumbing fault in the workflow, not an API blip: the
    step exits loud and names the variable, rather than reporting a silent
    `hit=false` that reads as "nothing to reuse"."""
    assert reuse.REQUIRED_ENV, "no required inputs read — every case below is vacuous"
    with FakeActionsArtifacts(tmp_path) as server:
        _seed(server, 55, _bundle_zip(CURRENT_HEAD))
        with pytest.raises(SystemExit):
            _run_reuse(server, tmp_path, monkeypatch, drop=missing)
    assert f"{missing} required" in capsys.readouterr().err
