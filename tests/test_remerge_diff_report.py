"""The merge-delta detector is what makes unattended conflict resolution safe.

A merge commit's tree is authored freely, so a resolution can introduce content
present in neither parent, and no ordinary one-parent diff shows it. These cases
drive real git repositories rather than stubbing git, because the whole question
is what `--remerge-diff` reports about real trees.

The two failure directions are not symmetric. A FALSE NEGATIVE — an invented
line the report omits — is the one that costs a merge, so the evil-merge case is
the load-bearing assertion here. A false positive only costs a human a read.
"""

import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    / ".github"
    / "resolver"
    / "remerge-diff-report.py"
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def commit(repo: Path, path: str, text: str, message: str) -> str:
    (repo / path).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    return r


def report(repo: Path, base: str, head: str, **env: str) -> str:
    res = subprocess.run(
        ["python3", str(SCRIPT)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "BASE_SHA": base,
            "HEAD_SHA": head,
            **env,
        },
    )
    return res.stdout


def conflicting_merge(
    repo: Path, ours: str, theirs: str, name: str = "f.txt"
) -> tuple[str, str]:
    """Build two branches that conflict on `name`, leaving the merge in
    progress. Returns (base_sha, merge_head_ref)."""
    base = commit(repo, name, "one\ntwo\nthree\n", "base")
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, name, theirs, "side change")
    git(repo, "checkout", "-q", "main")
    commit(repo, name, ours, "main change")
    res = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-edit", "side"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0, "fixture must actually conflict"
    return base, "side"


def test_an_invented_line_is_reported(repo: Path):
    # The resolution keeps both sides AND adds a line neither parent ever had.
    # This is the evil merge. Missing it is the failure that costs a merge.
    base, _ = conflicting_merge(repo, "one\nOURS\nthree\n", "one\nTHEIRS\nthree\n")
    (repo / "f.txt").write_text(
        "one\nOURS\nTHEIRS\nINVENTED\nthree\n", encoding="utf-8"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    head = git(repo, "rev-parse", "HEAD").strip()

    out = report(repo, base, head)
    assert "INVENTED" in out, "the detector missed content present in neither parent"
    assert "Hand-authored merge-resolution deltas" in out


def test_an_ordinary_resolution_taking_both_sides_is_retired(repo: Path):
    # Both sides' own lines, nothing else. Every block traces to a parent, so
    # nothing needs a human — this is the false-positive direction.
    base, _ = conflicting_merge(repo, "one\nOURS\nthree\n", "one\nTHEIRS\nthree\n")
    (repo / "f.txt").write_text("one\nOURS\nTHEIRS\nthree\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    head = git(repo, "rev-parse", "HEAD").strip()

    assert report(repo, base, head).strip() == ""


def test_a_derived_file_keeps_every_hunk_for_the_reviewer(repo: Path):
    # Tracing answers each hunk ALONE. For a file git must never line-merge
    # (`-merge`), hunks that each match a parent still combine into bytes no
    # generator produces — one side's entries beside the other's. The identical
    # resolution retires in `f.txt` above, so this pins the attribute, not the
    # content.
    commit(repo, ".gitattributes", "pnpm-lock.yaml -merge\n", "attrs")
    base, _ = conflicting_merge(
        repo, "one\nOURS\nthree\n", "one\nTHEIRS\nthree\n", name="pnpm-lock.yaml"
    )
    (repo / "pnpm-lock.yaml").write_text("one\nOURS\nTHEIRS\nthree\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    head = git(repo, "rev-parse", "HEAD").strip()

    out = report(repo, base, head)
    assert "**Derived from the merged tree:**" in out, out
    assert "THEIRS" in out, "the delta must reach the reviewer"


def test_a_rule_declared_only_on_the_pr_side_is_still_derived(repo: Path):
    # The renderer runs from the base checkout and reads the PR head as git
    # objects, so a `-merge` rule the PR itself adds is absent from the working
    # tree's attributes. Reading them at the head too is what covers it.
    base, _ = conflicting_merge(
        repo, "one\nOURS\nthree\n", "one\nTHEIRS\nthree\n", name="pnpm-lock.yaml"
    )
    (repo / "pnpm-lock.yaml").write_text("one\nOURS\nTHEIRS\nthree\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    commit(repo, ".gitattributes", "pnpm-lock.yaml -merge\n", "attrs")
    head = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "checkout", "-q", base)  # the base checkout, without the rule

    out = report(repo, base, head)
    assert "**Derived from the merged tree:**" in out, out


def test_a_rule_declared_only_at_the_merge_is_still_derived(repo: Path):
    # A rule the resolution itself declares, and a later commit drops, sits in
    # NEITHER range endpoint — only the merge's own tree carries it.
    base, _ = conflicting_merge(
        repo, "one\nOURS\nthree\n", "one\nTHEIRS\nthree\n", name="pnpm-lock.yaml"
    )
    (repo / "pnpm-lock.yaml").write_text("one\nOURS\nTHEIRS\nthree\n", encoding="utf-8")
    (repo / ".gitattributes").write_text("pnpm-lock.yaml -merge\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    (repo / ".gitattributes").unlink()
    commit(repo, "f.txt", "unrelated\n", "drop the rule")
    head = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "checkout", "-q", base)  # the base checkout, without the rule

    out = report(repo, base, head)
    assert "**Derived from the merged tree:**" in out, out


def test_a_backtick_in_a_derived_path_cannot_close_its_code_span(repo: Path):
    # The note sits OUTSIDE the diff fence, where the reviewer trusts it, and
    # the path is PR-authored. A raw backtick would end the span and land the
    # rest of the name as live markdown.
    name = "we`ird-lock.yaml"
    commit(repo, ".gitattributes", f'"{name}" -merge\n', "attrs")
    base, _ = conflicting_merge(
        repo, "one\nOURS\nthree\n", "one\nTHEIRS\nthree\n", name=name
    )
    (repo / name).write_text("one\nOURS\nTHEIRS\nthree\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    head = git(repo, "rev-parse", "HEAD").strip()

    out = report(repo, base, head)
    note = next(ln for ln in out.split("\n") if "Derived from the merged tree" in ln)
    assert "we'ird-lock.yaml" in note, note
    assert note.count("`") % 2 == 0, note


def test_a_derived_file_whose_head_bytes_equal_a_parent_still_reports(repo: Path):
    # Supersession retires a file whose head bytes equal a parent's exactly.
    # For a derived file that is the failure itself: one side's manifest beside
    # the other side's lock, which no install reproduces. `-merge` leaves the
    # mechanical merge at OURS, so resolving to THEIRS is a real delta whose
    # head bytes are parent 2's.
    commit(repo, ".gitattributes", "pnpm-lock.yaml -merge\n", "attrs")
    base, _ = conflicting_merge(
        repo, "one\nOURS\nthree\n", "one\nTHEIRS\nthree\n", name="pnpm-lock.yaml"
    )
    (repo / "pnpm-lock.yaml").write_text("one\nTHEIRS\nthree\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    head = git(repo, "rev-parse", "HEAD").strip()

    out = report(repo, base, head)
    assert "**Derived from the merged tree:**" in out, out
    assert "THEIRS" in out, "the delta must reach the reviewer"


def test_an_ordinary_file_beside_a_derived_one_still_retires(repo: Path):
    # The control: the attribute file is present and names another path, so a
    # regression that treats every path as derived is caught here rather than
    # reading as the rule working.
    commit(repo, ".gitattributes", "pnpm-lock.yaml -merge\n", "attrs")
    base, _ = conflicting_merge(repo, "one\nOURS\nthree\n", "one\nTHEIRS\nthree\n")
    (repo / "f.txt").write_text("one\nOURS\nTHEIRS\nthree\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    head = git(repo, "rev-parse", "HEAD").strip()

    assert report(repo, base, head).strip() == ""


def test_a_resolution_corrected_by_a_later_commit_is_retired(repo: Path):
    # A pushed merge's remerge-diff never changes, so a follow-up commit is the
    # only correction available. Without this the corrected resolution could
    # never clear, and the report would nag forever.
    base, _ = conflicting_merge(repo, "one\nOURS\nthree\n", "one\nTHEIRS\nthree\n")
    (repo / "f.txt").write_text(
        "one\nOURS\nTHEIRS\nINVENTED\nthree\n", encoding="utf-8"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    merge = git(repo, "rev-parse", "HEAD").strip()

    flagged = report(repo, base, merge)
    assert "INVENTED" in flagged, "precondition: it must be flagged before the fix"

    commit(repo, "f.txt", "one\nOURS\nTHEIRS\nthree\n", "drop the invented line")
    head = git(repo, "rev-parse", "HEAD").strip()
    assert report(repo, base, head).strip() == ""


def test_a_deletion_the_resolution_made_alone_is_reported(repo: Path):
    # The directional half of the trace: a line BOTH parents still carry, which
    # the resolution dropped. Base count is not greater than the parents', so it
    # must stay under review. This is a guardrail silently removed via a merge.
    base = commit(repo, "f.txt", "keep\nGUARD\ntail\n", "base")
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "f.txt", "keep\nGUARD\ntail\nside\n", "side appends")
    git(repo, "checkout", "-q", "main")
    commit(repo, "f.txt", "keep\nGUARD\ntail\nmain\n", "main appends")
    res = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-edit", "side"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0
    # Resolve, but silently drop GUARD — which neither side touched.
    (repo / "f.txt").write_text("keep\ntail\nside\nmain\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    head = git(repo, "rev-parse", "HEAD").strip()

    out = report(repo, base, head)
    assert "GUARD" in out, "a line neither parent removed was dropped and not reported"


def test_the_provenance_block_names_both_sides(repo: Path):
    # The downstream reviewer has no shell and cannot read the parents, so
    # without this block a deliberate removal and a dropped line are identical.
    base, _ = conflicting_merge(repo, "one\nOURS\nthree\n", "one\nTHEIRS\nthree\n")
    (repo / "f.txt").write_text(
        "one\nOURS\nTHEIRS\nINVENTED\nthree\n", encoding="utf-8"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    head = git(repo, "rev-parse", "HEAD").strip()

    out = report(repo, base, head)
    assert "Which side changed each file" in out
    assert "parent 1:" in out and "parent 2:" in out
    assert "main change" in out and "side change" in out


def test_shas_out_lists_only_the_merges_that_survived(repo: Path, tmp_path: Path):
    base, _ = conflicting_merge(repo, "one\nOURS\nthree\n", "one\nTHEIRS\nthree\n")
    (repo / "f.txt").write_text(
        "one\nOURS\nTHEIRS\nINVENTED\nthree\n", encoding="utf-8"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    head = git(repo, "rev-parse", "HEAD").strip()

    out_file = tmp_path / "shas.txt"
    subprocess.run(
        ["python3", str(SCRIPT), "--shas-out", str(out_file)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "BASE_SHA": base,
            "HEAD_SHA": head,
        },
    )
    assert out_file.read_text(encoding="utf-8").split() == [head]


def test_an_octopus_merge_fails_loud(repo: Path):
    # Skipping it silently would report "nothing to review" about exactly the
    # commit shape that cannot be reconstructed.
    base = commit(repo, "f.txt", "base\n", "base")
    for name in ("a", "b"):
        git(repo, "checkout", "-q", "-b", name, base)
        commit(repo, f"{name}.txt", name, f"{name} file")
    git(repo, "checkout", "-q", "main")
    # main needs a commit of its own, or `git merge a b` fast-forwards to `a`
    # first and lands a two-parent merge instead of an octopus.
    commit(repo, "main.txt", "main", "main file")
    git(repo, "merge", "--no-edit", "-q", "a", "b")
    head = git(repo, "rev-parse", "HEAD").strip()
    assert len(git(repo, "rev-list", "--parents", "-n1", head).split()) == 4

    res = subprocess.run(
        ["python3", str(SCRIPT)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "BASE_SHA": base,
            "HEAD_SHA": head,
        },
    )
    assert res.returncode != 0
    assert "octopus" in res.stderr


def test_the_cap_is_off_unless_asked_for(repo: Path):
    # The readers that audit have no size limit; only the PR comment does. A
    # merge dropped from what they read is a merge nobody looks at.
    base, _ = conflicting_merge(repo, "one\nOURS\nthree\n", "one\nTHEIRS\nthree\n")
    (repo / "f.txt").write_text(
        "one\nOURS\nTHEIRS\nINVENTED\nthree\n", encoding="utf-8"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    head = git(repo, "rev-parse", "HEAD").strip()

    assert "INVENTED" in report(repo, base, head)
    capped = report(repo, base, head, REMERGE_REPORT_MAX_BYTES="200")
    assert "INVENTED" not in capped and "omitted" in capped


def _binary_conflict_merge(repo: Path, resolution: str) -> tuple[str, str]:
    """A merge that conflicts on a BINARY path and on a text one.

    git renders the binary path as a section carrying only `remerge ` lines and
    no content — the shape `_notice_lines` classifies as a notice. `resolution`
    is what the text path is resolved to, so a caller can make the text half
    retire while the notice stands.
    """
    (repo / "blob.bin").write_bytes(b"\x00\x01BASE\x02\x00")
    base = commit(repo, "f.txt", "one\ntwo\nthree\n", "base")
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "blob.bin").write_bytes(b"\x00\x01SIDE\x02\x00")
    commit(repo, "f.txt", "one\nTHEIRS\nthree\n", "side change")
    git(repo, "checkout", "-q", "main")
    (repo / "blob.bin").write_bytes(b"\x00\x01MAIN\x02\x00")
    commit(repo, "f.txt", "one\nOURS\nthree\n", "main change")
    subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-edit", "side"],
        capture_output=True,
        check=False,
    )
    (repo / "f.txt").write_text(resolution, encoding="utf-8")
    (repo / "blob.bin").write_bytes(b"\x00\x01MAIN\x02\x00")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    return base, git(repo, "rev-parse", "HEAD").strip()


def test_a_path_git_could_not_merge_is_reported(repo: Path):
    """git's conflict notice names where it gave up, which is where a wrong
    resolution is most likely. It carries no hunk by construction, so a filter
    that reads only the diff must not drop the section carrying it."""
    base, head = _binary_conflict_merge(repo, "one\nOURS\nTHEIRS\nINVENTED\nthree\n")
    out = report(repo, base, head)
    assert "Paths the mechanical merge could not resolve" in out
    assert "blob.bin" in out


def test_a_notice_survives_when_every_content_hunk_retires(repo: Path):
    """The regression this pins: the text path's delta traces to the parents and
    retires, leaving an empty diff, while the binary path's notice stands. A
    suppression keyed on the diff alone renders nothing and hides the merge from
    the sticky comment and from self_review.py alike."""
    # Both sides' lines, which the tracing filter retires as ordinary.
    base, head = _binary_conflict_merge(repo, "one\nOURS\nTHEIRS\nthree\n")
    out = report(repo, base, head)
    assert "Paths the mechanical merge could not resolve" in out, (
        "a merge git could not fully merge must not render as nothing"
    )
    assert "blob.bin" in out


def test_a_fully_retired_merge_with_no_notice_still_renders_nothing(repo: Path):
    """The other half of the same rule, so restoring the notice does not undo
    it: self_review.py reads a non-empty report as work to do."""
    base, _ = conflicting_merge(repo, "one\nOURS\nthree\n", "one\nTHEIRS\nthree\n")
    (repo / "f.txt").write_text("one\nOURS\nTHEIRS\nthree\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    head = git(repo, "rev-parse", "HEAD").strip()
    assert report(repo, base, head).strip() == ""


def test_a_derived_path_keeps_its_anti_false_positive_notes(repo: Path):
    """A derived path is exempt from TRACING, not from every pass.

    Suppressing the whole annotation pass also drops the notes that exist to
    stop a wrong finding: `Corrected at head:` says an added line does not ship,
    and `Still in the merged file:` says a removed one only moved. A lockfile is
    the worst case, since a line reappearing verbatim elsewhere is ordinary.
    """
    commit(repo, ".gitattributes", "pnpm-lock.yaml -merge\n", "attrs")
    base, _ = conflicting_merge(
        repo, "one\nOURS\nthree\n", "one\nTHEIRS\nthree\n", name="pnpm-lock.yaml"
    )
    (repo / "pnpm-lock.yaml").write_text(
        "one\nOURS\nTHEIRS\nINVENTED\nthree\n", encoding="utf-8"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    # A later commit undoes the resolution, exactly as a correction would.
    commit(
        repo, "pnpm-lock.yaml", "one\nOURS\nTHEIRS\nthree\n", "drop the invented line"
    )
    head = git(repo, "rev-parse", "HEAD").strip()

    out = report(repo, base, head)
    assert "**Derived from the merged tree:**" in out, out
    assert "**Corrected at head:**" in out, (
        "a derived path must still get the notes that prevent a wrong finding"
    )
