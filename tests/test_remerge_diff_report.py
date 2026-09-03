"""The merge-delta detector is what makes unattended conflict resolution safe.

A merge commit's tree is authored freely, so a resolution can introduce content
present in neither parent, and no ordinary one-parent diff shows it. These cases
drive real git repositories rather than stubbing git, because the whole question
is what `--remerge-diff` reports about real trees.

The two failure directions are not symmetric. A FALSE NEGATIVE — an invented
line the report omits — is the one that costs a merge, so the evil-merge case is
the load-bearing assertion here. A false positive only costs a human a read.
"""

import importlib.util
import os
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


def test_a_line_a_parent_added_ELSEWHERE_does_not_excuse_it_here(repo: Path):
    """The evil merge a whole-blob occurrence COUNT cannot see. `side` really
    did add `GUARD()` — at the far end of the file, for its own reasons. The
    resolution then inserts one at the conflict site, where nobody put it.
    Counting says a parent has more of these than the base did and retires the
    hunk; anchored, `side` never added it AFTER `one`, so it stays."""
    base = commit(repo, "f.txt", "one\ntwo\nthree\nfour\n", "base")
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "f.txt", "one\nTHEIRS\nthree\nfour\nGUARD()\n", "side change")
    git(repo, "checkout", "-q", "main")
    commit(repo, "f.txt", "one\nOURS\nthree\nfour\n", "main change")
    res = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-edit", "side"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0, "fixture must actually conflict"
    (repo / "f.txt").write_text(
        "one\nGUARD()\nOURS\nTHEIRS\nthree\nfour\nGUARD()\n", encoding="utf-8"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    head = git(repo, "rev-parse", "HEAD").strip()

    assert "GUARD()" in report(repo, base, head), (
        "a line a parent added somewhere else retired an insertion nobody made here"
    )


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


def _rule_table(tmp_path: Path, owned: str, rederived: str) -> str:
    """A stand-in caller rule table answering only the ownership queries. Any
    `--root=` run exits 1, so a path this table retires without re-derivation
    would come BACK as a kept hunk if the renderer regenerated after all."""
    mjs = tmp_path / "rules.mjs"
    mjs.write_text(
        "const a = process.argv.slice(2);\n"
        'if (a.includes("--owned")) {\n'
        f'  const out = a.includes("--rederived-only") ? {rederived!r} : {owned!r};\n'
        "  if (out) console.log(out);\n"
        "  process.exit(0);\n"
        "}\n"
        "process.exit(1);\n",
        encoding="utf-8",
    )
    return str(mjs)


def _derived_conflict_beside_a_source_edit(repo: Path) -> tuple[str, str]:
    """A merge that conflicts on a `-merge` file `gen.lock` (resolved to bytes
    neither parent holds, as a regeneration produces) AND on `f.txt` (resolved
    with an invented line). Returns (base_sha, head_sha)."""
    commit(repo, ".gitattributes", "gen.lock -merge\n", "attrs")
    commit(repo, "gen.lock", "g-one\ng-two\n", "gen base")
    base, _ = conflicting_merge(repo, "one\nOURS\nthree\n", "one\nTHEIRS\nthree\n")
    (repo / "f.txt").write_text("one\nOURS\nTHEIRS\nINVENTED\nthree\n", "utf-8")
    (repo / "gen.lock").write_text("g-one\nGENERATED-JUNK\n", "utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    return base, git(repo, "rev-parse", "HEAD").strip()


def test_a_derived_file_a_check_rederives_retires_whole(repo: Path, tmp_path: Path):
    # `-merge` only says no PARENT'S bytes can vouch for the file; a required
    # check re-deriving it from source judges the merged tree itself, which is
    # the whole-file answer the derived note asks for. The source hunk beside
    # it must still reach the reviewer.
    base, head = _derived_conflict_beside_a_source_edit(repo)
    table = _rule_table(tmp_path, owned="gen.lock", rederived="gen.lock")

    out = report(
        repo,
        base,
        head,
        AUTO_RESOLVE_RESOLVER_MJS=table,
        PATH=os.environ["PATH"],
    )
    assert "**Generator-owned:**" in out, out
    assert "GENERATED-JUNK" not in out, "a check-rederived delta reached the reviewer"
    assert "**Derived from the merged tree:**" not in out, out
    assert "INVENTED" in out, "the source hunk beside it must still be read"


def test_an_all_generated_resolution_renders_nothing(repo: Path, tmp_path: Path):
    # The resolve job's self-review reads a non-empty report as "something to
    # review" and spends a model run on it, so a resolution made ONLY of
    # check-rederived files must render nothing at all.
    commit(repo, ".gitattributes", "gen.lock -merge\n", "attrs")
    base, _ = conflicting_merge(
        repo, "one\nOURS\nthree\n", "one\nTHEIRS\nthree\n", name="gen.lock"
    )
    (repo / "gen.lock").write_text("one\nGENERATED-JUNK\nthree\n", "utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    head = git(repo, "rev-parse", "HEAD").strip()
    table = _rule_table(tmp_path, owned="gen.lock", rederived="gen.lock")

    out = report(
        repo,
        base,
        head,
        AUTO_RESOLVE_RESOLVER_MJS=table,
        PATH=os.environ["PATH"],
    )
    assert out.strip() == "", out


def test_a_pre_pass_verified_lockfile_retires_without_rederiving(
    repo: Path, tmp_path: Path
):
    # No check re-derives a lockfile, so it normally needs the scratch-worktree
    # regeneration. AUTO_RESOLVE_PRE_PASS_VERIFIED says bundle.py's own
    # `--verify` post-condition already compared these bytes in this job; the
    # rule table here fails any `--root=` run, so a kept GENERATED-JUNK hunk
    # would prove the renderer regenerated after all.
    base, head = _derived_conflict_beside_a_source_edit(repo)
    table = _rule_table(tmp_path, owned="gen.lock", rederived="")

    out = report(
        repo,
        base,
        head,
        AUTO_RESOLVE_RESOLVER_MJS=table,
        AUTO_RESOLVE_VERIFY_REGENERATED="true",
        AUTO_RESOLVE_PRE_PASS_VERIFIED="true",
        PATH=os.environ["PATH"],
    )
    assert "**Regenerated (verified):**" in out, out
    assert "GENERATED-JUNK" not in out, "an unrederived lockfile delta was kept"
    assert "INVENTED" in out, "the source hunk beside it must still be read"


def test_a_lockfile_without_the_pre_pass_flag_stays_in_the_review(
    repo: Path, tmp_path: Path
):
    # The fail-toward-review direction the pre-pass flag rests on: nothing has
    # compared these bytes (this table fails every `--root=` run), so the delta
    # is kept rather than retired on a claim nobody made.
    base, head = _derived_conflict_beside_a_source_edit(repo)
    table = _rule_table(tmp_path, owned="gen.lock", rederived="")

    out = report(
        repo,
        base,
        head,
        AUTO_RESOLVE_RESOLVER_MJS=table,
        AUTO_RESOLVE_VERIFY_REGENERATED="true",
        PATH=os.environ["PATH"],
    )
    assert "**Regenerated (verified):**" not in out, out
    assert "GENERATED-JUNK" in out, "an unverified lockfile delta was retired"
    assert "**Regenerated output does NOT match:**" not in out, (
        "a derived path must not carry the hunk-read note beside the whole-file one"
    )


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


def test_a_PARTLY_undone_resolution_stays_in_the_report(repo: Path):
    # `_line_runs` joins consecutive added lines into ONE block, so a comment
    # above a smuggled line is a single unit. A later commit that merely rewords
    # the comment drops the block's count to zero while the smuggled line still
    # ships, and the still-shipping delta would leave the report unread.
    base, _ = conflicting_merge(repo, "one\nOURS\nthree\n", "one\nTHEIRS\nthree\n")
    (repo / "f.txt").write_text(
        "one\nOURS\nTHEIRS\n# why we do this\nDANGEROUS=1\nthree\n",
        encoding="utf-8",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    merge = git(repo, "rev-parse", "HEAD").strip()
    assert "DANGEROUS" in report(repo, base, merge), (
        "precondition: flagged at the merge"
    )

    commit(
        repo,
        "f.txt",
        "one\nOURS\nTHEIRS\n# reworded rationale\nDANGEROUS=1\nthree\n",
        "reword only the comment",
    )
    head = git(repo, "rev-parse", "HEAD").strip()
    assert "DANGEROUS" in report(repo, base, head)


@pytest.mark.parametrize(
    ("base", "parent", "retired"),
    [
        ("A\nX\nY\n", "X\nY\nA\nGUARD\n", False),
        ("A\nX\nY\n", "A\nGUARD\nX\nY\n", True),
        ("P\nA\nX\nY\n", "X\nY\nP\nA\nGUARD\n", False),
        ("P\nA\nX\nY\n", "P\nA\nGUARD\nX\nY\n", True),
    ],
    ids=[
        "parent moved the anchor",
        "anchor stayed put",
        "parent moved the anchor WITH its predecessor",
        "anchor stayed put, with a predecessor",
    ],
)
def test_an_anchor_a_parent_MOVED_does_not_retire_the_hunk(
    base: str, parent: str, retired: bool
) -> None:
    """Counts cannot tell an anchor that stayed and gained a line from one the
    parent moved and gained a line at its new home: every count is 1 either way,
    and retiring the moved case clears an insertion the parent made somewhere
    else. The predecessor cases pin what the preceding line alone cannot see —
    `P` precedes `A` on both sides, so only `X` and `Y` crossing the anchor
    names the move."""
    m = _novelty()
    blobs = m.ParentBlobs(base=base, parent1=parent, parent2=base)
    hunk = "@@ -1,3 +1,4 @@\n A\n+GUARD\n X\n"
    assert m.hunk_traced_to_the_parents(hunk, blobs) is retired


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


# ── the retirement predicates, driven directly ───────────────────────────────
# `.github/resolver/_merge_delta_novelty.py` holds the predicates the renderer
# above calls. The `report()` cases reach them through a real merge; these reach
# them with the three reference texts spelled out, which is the only way to pin
# a case a git merge cannot be made to produce.
def _resolver_module(name: str):
    """`.github/resolver/NAME.py` loaded as a module."""
    spec = importlib.util.spec_from_file_location(name, SCRIPT.parent / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _novelty():
    return _resolver_module("_merge_delta_novelty")


def _lockentries():
    return _resolver_module("_shared_lock_entries")


_HUNK = "@@ -1,3 +1,4 @@\n one\n+GUARD()\n two\n three\n"


def test_bundle_novelty_refuses_a_line_a_parent_added_elsewhere():
    m = _novelty()
    blobs = m.ParentBlobs(
        base="one\ntwo\nthree\n",
        # The parent really did add GUARD() — at the far end, not after `one`.
        parent1="one\ntwo\nthree\nGUARD()\n",
        parent2="one\ntwo\nthree\n",
    )
    assert m.hunk_traced_to_the_parents(_HUNK, blobs) is False


def test_bundle_novelty_still_retires_the_same_addition_made_here():
    m = _novelty()
    blobs = m.ParentBlobs(
        base="one\ntwo\nthree\n",
        parent1="one\nGUARD()\ntwo\nthree\n",
        parent2="one\ntwo\nthree\n",
    )
    assert m.hunk_traced_to_the_parents(_HUNK, blobs) is True


def test_bundle_novelty_refuses_an_anchor_a_parent_created_by_deleting():
    """An anchor is not proof of an addition: a parent that merely DROPPED the
    line between `A` and `GUARD` makes them newly adjacent, so an anchored count
    alone would clear a second `GUARD` nobody added. The bare run answers "a
    parent added this text at all" and refuses it."""
    m = _novelty()
    hunk = "@@ -1,2 +1,3 @@\n A\n+GUARD\n GUARD\n"
    blobs = m.ParentBlobs(
        base="A\nOLD\nGUARD\n",
        parent1="A\nGUARD\n",
        parent2="A\nOLD\nGUARD\n",
    )
    assert m.hunk_traced_to_the_parents(hunk, blobs) is False


_REMOVAL_HUNK = "@@ -1,4 +1,3 @@\n one\n-GUARD()\n two\n three\n"


def test_bundle_novelty_refuses_a_line_a_parent_deleted_elsewhere():
    """The removal half of the same question. Unanchored, base holding two
    `GUARD()` against a parent holding one retires the hunk on a count alone —
    even though the copy the parent dropped is not the copy the resolution did."""
    m = _novelty()
    blobs = m.ParentBlobs(
        base="one\nGUARD()\ntwo\nthree\nGUARD()\n",
        parent1="one\nGUARD()\ntwo\nthree\n",
        parent2="one\nGUARD()\ntwo\nthree\nGUARD()\n",
    )
    assert m.hunk_traced_to_the_parents(_REMOVAL_HUNK, blobs) is False


def test_bundle_novelty_refuses_a_split_parent_trace():
    """Both halves must come from ONE parent. Parent 1 adds the text elsewhere
    and parent 2 merely deletes the line between the anchor and it, so each
    answers one half and neither wrote the insertion."""
    m = _novelty()
    hunk = "@@ -1,2 +1,3 @@\n A\n+GUARD\n GUARD\n"
    blobs = m.ParentBlobs(
        base="A\nOLD\nGUARD\n",
        parent1="A\nOLD\nGUARD\nGUARD\n",
        parent2="A\nGUARD\n",
    )
    assert m.hunk_traced_to_the_parents(hunk, blobs) is False


def test_bundle_novelty_refuses_a_run_with_no_anchor():
    """A run opening the hunk has no neighbour to anchor to, and a bare block is
    the location-agnostic comparison the anchor replaces — so it never traces."""
    m = _novelty()
    hunk = "@@ -0,0 +1,2 @@\n+GUARD()\n one\n"
    blobs = m.ParentBlobs(base="one\n", parent1="one\nGUARD()\n", parent2="one\n")
    assert m.hunk_traced_to_the_parents(hunk, blobs) is False


def test_bundle_novelty_never_joins_a_run_across_a_conflict_marker():
    """A marker BREAKS a run. Spliced, `A1` + `B1` becomes one block that traces
    to a parent holding both, retiring a hunk neither run traces on its own."""
    m = _novelty()
    hunk = "@@ -1,2 +1,5 @@\n one\n+A1\n+<<<<<<< HEAD\n+B1\n two\n"
    blobs = m.ParentBlobs(
        base="one\ntwo\n", parent1="one\nA1\nB1\ntwo\n", parent2="one\ntwo\n"
    )
    assert m.hunk_traced_to_the_parents(hunk, blobs) is False


def test_bundle_novelty_refuses_two_unrelated_edits_by_one_parent():
    """One parent can raise both counts through two edits at different sites:
    an appended `GUARD` raises the bare count while deleting `OLD` raises the
    anchored one. Requiring exactly one occurrence names a single site."""
    m = _novelty()
    hunk = "@@ -1,2 +1,3 @@\n A\n+GUARD\n GUARD\n"
    blobs = m.ParentBlobs(
        base="A\nOLD\nGUARD\nX\n",
        parent1="A\nGUARD\nX\nGUARD\n",
        parent2="A\nOLD\nGUARD\nX\n",
    )
    assert m.hunk_traced_to_the_parents(hunk, blobs) is False


def test_bundle_novelty_refuses_an_ambiguous_anchor_line():
    """A repeated anchor names no site. The parent added `GUARD` after the
    SECOND `A` and the resolution added it after the FIRST, yet both the run and
    its anchored form occur exactly once — only the anchor LINE's own count
    separates them."""
    m = _novelty()
    hunk = "@@ -1,4 +1,5 @@\n A\n+GUARD\n X\n A\n Y\n"
    blobs = m.ParentBlobs(
        base="A\nX\nA\nY\n",
        parent1="A\nX\nA\nGUARD\nY\n",
        parent2="A\nX\nA\nY\n",
    )
    assert m.hunk_traced_to_the_parents(hunk, blobs) is False


def test_bundle_novelty_refuses_an_anchor_made_unique_by_a_deletion():
    """A parent can make its own anchor unique by deleting the other copy. Base
    `A / X / A / Y`, a parent that drops the FIRST `A` and adds `GUARD` after the
    survivor, a resolution that adds `GUARD` after the first: the parent holds
    one `A`, so an anchor counted on the parent alone names a site the
    resolution never touched."""
    m = _novelty()
    hunk = "@@ -1,4 +1,5 @@\n A\n+GUARD\n X\n A\n Y\n"
    blobs = m.ParentBlobs(
        base="A\nX\nA\nY\n",
        parent1="X\nA\nGUARD\nY\n",
        parent2="A\nX\nA\nY\n",
    )
    assert m.hunk_traced_to_the_parents(hunk, blobs) is False


def test_bundle_novelty_retires_a_run_a_parent_added_with_its_own_anchor():
    """The other side of that rule: an anchor absent from the base came WITH the
    run, so no earlier occurrence competes with it and the site is identified."""
    m = _novelty()
    hunk = "@@ -1,2 +1,3 @@\n NEW\n+GUARD\n Y\n"
    blobs = m.ParentBlobs(
        base="Y\n",
        parent1="NEW\nGUARD\nY\n",
        parent2="Y\n",
    )
    assert m.hunk_traced_to_the_parents(hunk, blobs) is True


_CONFLICT_HUNK = (
    "@@ -1,7 +1,2 @@\n one\n-<<<<<<< a (drop two)\n-three\n-=======\n"
    "-two\n THREE\n->>>>>>> b (shout three)\n"
)


def test_bundle_novelty_retires_a_side_git_itself_delimited():
    """Inside a conflict every run is marker-adjacent, so no anchor exists and
    demanding one refuses the whole population this instrument reads. Git chose
    the position there; the resolution chose only which side to keep."""
    m = _novelty()
    blobs = m.ParentBlobs(
        base="one\ntwo\nthree\n", parent1="one\nthree\n", parent2="one\ntwo\nTHREE\n"
    )
    assert m.hunk_traced_to_the_parents(_CONFLICT_HUNK, blobs) is True


def test_bundle_novelty_refuses_a_conflict_side_NEITHER_parent_wrote():
    """The fail-closed direction inside a conflict: dropping the anchor there
    must not drop the parent comparison with it."""
    m = _novelty()
    invented = _CONFLICT_HUNK.replace("-three\n", "-INVENTED\n")
    blobs = m.ParentBlobs(
        base="one\ntwo\nthree\n", parent1="one\nthree\n", parent2="one\ntwo\nTHREE\n"
    )
    assert m.hunk_traced_to_the_parents(invented, blobs) is False


def test_bundle_novelty_refuses_an_anchor_BOTH_parents_introduced() -> None:
    """An anchor absent from the base is not automatically this parent's: with
    base `X / M / Y / N`, parent 1 adding `A / GUARD` after `X` and parent 2
    adding `A` after `Y`, a resolution adding `GUARD` after parent 2's `A`
    matches parent 1's block at a site parent 1 never touched."""
    m = _novelty()
    hunk = "@@ -1,4 +1,5 @@\n A\n+GUARD\n N\n"
    blobs = m.ParentBlobs(
        base="X\nM\nY\nN\n",
        parent1="X\nA\nGUARD\nM\nY\nN\n",
        parent2="X\nM\nY\nA\nN\n",
    )
    assert m.hunk_traced_to_the_parents(hunk, blobs) is False


# ── the trailing-whitespace strip, and what the head still carries ───────────
# Two more retirement questions the renderer asks. The first is a predicate over
# the hunk alone; the second is a count handed to the reviewer when a file drops
# out of the fence, so no inference has to fill that gap.
_WS_STRIP = "@@ -1,2 +1,2 @@\n ctx\n-tail  \n+tail\n"
_WS_ADD = "@@ -1,2 +1,2 @@\n ctx\n-tail\n+tail  \n"
_REAL_EDIT = "@@ -1,2 +1,2 @@\n ctx\n-tail  \n+TAIL\n"
_UNPAIRED = "@@ -1,3 +1,2 @@\n ctx\n-tail  \n-gone\n+tail\n"
_WS_MOVED = "@@ -1,3 +1,3 @@\n-tail  \n ctx\n+tail\n"
_NBSP = "@@ -1,2 +1,2 @@\n ctx\n-tail \n+tail\n"


@pytest.mark.parametrize(
    ("hunk", "retired", "why"),
    [
        (_WS_STRIP, True, "a strip is what a commit-time whitespace guard forces"),
        (_WS_ADD, False, "adding trailing whitespace is nobody's mandate"),
        (_REAL_EDIT, False, "a changed word is not whitespace"),
        (_UNPAIRED, False, "an unpaired removal is not a line-for-line strip"),
        (
            _WS_MOVED,
            False,
            "a context line between removed and added is a move, not a strip",
        ),
        (_NBSP, False, "a non-breaking space is not whitespace any guard strips"),
    ],
)
def test_only_a_pure_trailing_whitespace_strip_retires(hunk, retired, why):
    # The refusing directions are the point: a predicate driven only by the
    # agreeing case stays green after the direction check is deleted.
    assert _novelty().hunk_strips_trailing_whitespace(hunk) is retired, why


def test_head_carriage_counts_whole_blocks_and_never_a_marker():
    """Blocks, not lines, and a conflict marker is in none of them.

    Counting a marker into a block would make it match no revision of any file,
    so a run the head really carries would report as gone — and it is this
    note's TRUE answer that stands a reviewer down.
    """
    m = _novelty()
    hunk = "@@ -1,3 +1,3 @@\n one\n-GONE\n+KEPT\n two\n"
    head = "one\nKEPT\ntwo\n"

    assert m.blocks_carried_at_head(hunk, "+", head) == (1, 1)
    assert m.blocks_carried_at_head(hunk, "-", head) == (0, 1)
    marked = "@@ -1,3 +1,2 @@\n one\n-<<<<<<< HEAD\n-KEPT\n two\n"
    assert m.blocks_carried_at_head(marked, "-", head) == (1, 1)
    blank = "@@ -1,2 +1,3 @@\n one\n+\n two\n"
    assert m.blocks_carried_at_head(blank, "+", head) == (0, 0)


_PAD = "\n".join(f"pad{i}" for i in range(8))


def _strip_beside_an_invented_line(repo: Path, name: str) -> tuple[str, str]:
    """A merge whose resolution invents a line at the conflict AND, in a hunk of
    its own, strips the trailing whitespace neither side touched. The padding is
    what keeps the strip in a hunk of its own."""
    base = commit(repo, name, f"a\n{_PAD}\ntail  \n", "base")
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, name, f"THEIRS\n{_PAD}\ntail  \n", "side change")
    git(repo, "checkout", "-q", "main")
    commit(repo, name, f"OURS\n{_PAD}\ntail  \n", "main change")
    res = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-edit", "side"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0, "fixture must actually conflict"
    (repo / name).write_text(
        f"OURS\nTHEIRS\nINVENTED\n{_PAD}\ntail\n", encoding="utf-8"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    return base, git(repo, "rev-parse", "HEAD").strip()


@pytest.mark.parametrize(("name", "retired"), [("s.sh", True), ("x.patch", False)])
def test_a_strip_retires_only_where_the_CALLER_mandates_it(
    repo: Path, name: str, retired: bool
):
    """The mandate is read from the repository under report, never from the tree
    the renderer ships in. `*.patch -whitespace` here turns off `git diff
    --check` for that path, and a strip there CHANGES what the patch applies to,
    so its hunk stays in the fence."""
    commit(repo, ".gitattributes", "*.patch -whitespace\n", "attrs")
    base, head = _strip_beside_an_invented_line(repo, name)

    out = report(repo, base, head)
    assert "INVENTED" in out, out
    assert ("**Trailing whitespace only:**" in out) is retired, out
    assert ("-tail  " in out) is not retired, out


def test_a_superseded_file_reports_what_the_head_still_carries(repo: Path):
    """`Superseded at head:` says this resolution's delta does not SHIP. It never
    says the head lacks that content, and a reviewer who fills that gap by
    inference blocks on a line the head does carry. The counts are that
    evidence, and they must survive the file dropping out of the fence."""
    commit(repo, "lib.py", "self_signed_cert\n", "chore: add lib")
    base, _ = conflicting_merge(repo, "one\nOURS\nthree\n", "one\nTHEIRS\nthree\n")
    (repo / "f.txt").write_text(
        "one\nOURS\nTHEIRS\nINVENTED\nthree\n", encoding="utf-8"
    )
    # A hand edit to a file neither side touched: this merge's own delta.
    (repo / "lib.py").write_text("changed\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    head = commit(repo, "lib.py", "self_signed_cert\n", "fix: put the fixture back")

    out = report(repo, base, head)
    assert "**Superseded at head:** `lib.py`" in out, out
    line = next(
        ln for ln in out.split("\n") if ln.startswith("**Head carriage:** `lib.py`")
    )
    assert "added here the PR head carries 0" in line, line
    assert "of the 1 it removed the head still carries 1" in line, line


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


KEEP = "def keep():\n    return 0\n"
OURS_DUP = 'def dup():\n    return "main"\n'
THEIRS_DUP = 'def dup():\n    return "side"\n'
ONLY_SIDE = "def only_side():\n    return 1\n"


def _same_name_both_sides(repo: Path) -> str:
    """Both parents add a top-level `dup` to `t.py` with different bodies, and
    `side` adds a second function only it has. Leaves the merge in progress and
    returns the merge-base sha."""
    base = commit(repo, "t.py", KEEP, "base")
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "t.py", f"{KEEP}\n\n{THEIRS_DUP}\n\n{ONLY_SIDE}", "side adds two")
    git(repo, "checkout", "-q", "main")
    commit(repo, "t.py", f"{KEEP}\n\n{OURS_DUP}", "main adds dup")
    res = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-edit", "side"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0, "fixture must actually conflict"
    return base


def _resolve_as(repo: Path, text: str) -> str:
    (repo / "t.py").write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    return git(repo, "rev-parse", "HEAD").strip()


def test_a_name_both_parents_added_survives_once_and_the_drop_is_explained(repo: Path):
    """Python binds the LAST `def`, so a file holding both copies collects one
    and silently drops the other. The union has to delete one, and no parent's
    commit explains that deletion — the signal a reviewer reads as an evil
    merge, and the one that vetoed a correct resolution."""
    base = _same_name_both_sides(repo)
    head = _resolve_as(repo, f"{KEEP}\n\n{OURS_DUP}\n\n{ONLY_SIDE}")

    out = report(repo, base, head)
    assert "Deduplicated by the merge:" in out
    assert "`dup`" in out


def test_a_survivor_matching_NEITHER_parent_is_not_explained(repo: Path):
    """The fail-closed direction. The resolution rewrote the definition it kept,
    so its bytes are content neither parent wrote and nothing may retire the
    removal beside it."""
    base = _same_name_both_sides(repo)
    head = _resolve_as(
        repo, f'{KEEP}\n\ndef dup():\n    return "invented"\n\n\n{ONLY_SIDE}'
    )

    out = report(repo, base, head)
    assert out.strip(), "the fixture must still produce a report to annotate"
    assert "Deduplicated by the merge:" not in out


def test_a_removal_of_a_name_only_ONE_parent_defines_is_not_explained(repo: Path):
    """No collision, so dropping `dup` was a choice and stays under review."""
    base = commit(repo, "t.py", KEEP, "base")
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "t.py", f"{KEEP}\n\n{THEIRS_DUP}\n\n{ONLY_SIDE}", "side adds two")
    git(repo, "checkout", "-q", "main")
    commit(repo, "t.py", f"{KEEP}\n\ndef other():\n    return 2\n", "main adds other")
    subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-edit", "side"],
        capture_output=True,
        text=True,
        check=False,
    )
    head = _resolve_as(repo, f"{KEEP}\n\ndef other():\n    return 2\n\n\n{ONLY_SIDE}")

    out = report(repo, base, head)
    assert out.strip(), "the fixture must still produce a report to annotate"
    assert "Deduplicated by the merge:" not in out


_SHARED_LOCK = """[[package]]
name = "zipfile-zstd"
version = "1.0.0"
dependencies = [{ name = "zstandard", marker = "python_full_version < '3.14'" }]
"""
_OURS_LOCK_ADD = '[[package]]\nname = "ours-only"\nversion = "1.0.0"\n'
_THEIRS_LOCK_ADD = '[[package]]\nname = "theirs-only"\nversion = "1.0.0"\n'
_RELOCKED = _SHARED_LOCK.replace(", marker = \"python_full_version < '3.14'\"", "")


def _lock_conflict(repo: Path) -> str:
    """Both parents append a package to `uv.lock`, and both keep the shared
    entry byte for byte. Leaves the merge in progress."""
    base = commit(repo, "uv.lock", _SHARED_LOCK, "base")
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "uv.lock", f"{_SHARED_LOCK}\n{_THEIRS_LOCK_ADD}", "side locks one")
    git(repo, "checkout", "-q", "main")
    commit(repo, "uv.lock", f"{_SHARED_LOCK}\n{_OURS_LOCK_ADD}", "main locks one")
    res = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-edit", "side"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0, "fixture must actually conflict"
    return base


def _resolve_lock_as(repo: Path, text: str) -> str:
    (repo / "uv.lock").write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    return git(repo, "rev-parse", "HEAD").strip()


def test_a_relock_that_drops_a_pin_BOTH_parents_carried_is_named(repo: Path):
    """agent-glovebox #5562: the merge dropped `zstandard`'s marker from an entry
    both parents carried identically, so no conflict existed on it and no
    resolution choice was made. The solver moved it, and the lockfile decides
    what gets installed."""
    base = _lock_conflict(repo)
    head = _resolve_lock_as(repo, f"{_RELOCKED}\n{_OURS_LOCK_ADD}\n{_THEIRS_LOCK_ADD}")

    out = report(repo, base, head)
    assert "Both parents agreed:" in out
    assert "`zipfile-zstd`" in out


def test_a_relock_that_only_unions_the_two_sides_names_nothing(repo: Path):
    """The false-positive direction: every entry the parents shared survives
    untouched, so the note must stay silent."""
    base = _lock_conflict(repo)
    head = _resolve_lock_as(
        repo, f"{_SHARED_LOCK}\n{_OURS_LOCK_ADD}\n{_THEIRS_LOCK_ADD}"
    )

    out = report(repo, base, head)
    assert out.strip(), "the fixture must still produce a report to annotate"
    assert "Both parents agreed:" not in out


def test_a_name_the_MERGE_BASE_already_binds_is_not_a_collision(repo: Path):
    """Both parents EDITED an existing definition, so the copy the merge dropped
    is an ordinary resolution choice that may have discarded behaviour."""
    base = commit(repo, "t.py", f"{KEEP}\n\ndef dup():\n    return 0\n", "base")
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "t.py", f"{KEEP}\n\n{THEIRS_DUP}\n\n{ONLY_SIDE}", "side edits dup")
    git(repo, "checkout", "-q", "main")
    commit(repo, "t.py", f"{KEEP}\n\n{OURS_DUP}", "main edits dup")
    subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-edit", "side"],
        capture_output=True,
        text=True,
        check=False,
    )
    head = _resolve_as(repo, f"{KEEP}\n\n{OURS_DUP}\n\n{ONLY_SIDE}")

    out = report(repo, base, head)
    assert out.strip(), "the fixture must still produce a report to annotate"
    assert "Deduplicated by the merge:" not in out


def test_two_parents_adding_the_SAME_definition_still_names_the_collision(repo: Path):
    """The least ambiguous survivor case: the merged copy equals both parents,
    so the dropped copy is that same text and the removal is still forced."""
    base = commit(repo, "t.py", KEEP, "base")
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "t.py", f"{KEEP}\n\n{OURS_DUP}\n\n{ONLY_SIDE}", "side adds dup")
    git(repo, "checkout", "-q", "main")
    commit(
        repo, "t.py", f"{KEEP}\n\ndef other():\n    return 2\n\n\n{OURS_DUP}", "main"
    )
    subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-edit", "side"],
        capture_output=True,
        text=True,
        check=False,
    )
    head = _resolve_as(
        repo,
        f"{KEEP}\n\ndef other():\n    return 2\n\n\n{OURS_DUP}\n\n{ONLY_SIDE}",
    )

    out = report(repo, base, head)
    assert "Deduplicated by the merge:" in out
    assert "`dup`" in out


def test_a_lock_entry_the_head_has_put_back_is_not_named(repo: Path):
    """A later commit restored the drifted entry, so it no longer ships and the
    reviewer must not be sent to read it."""
    base = _lock_conflict(repo)
    relocked = _SHARED_LOCK.replace(", marker = \"python_full_version < '3.14'\"", "")
    _resolve_lock_as(repo, f"{relocked}\n{_OURS_LOCK_ADD}\n{_THEIRS_LOCK_ADD}")
    head = commit(
        repo,
        "uv.lock",
        f"{_SHARED_LOCK}\n{_OURS_LOCK_ADD}\n{_THEIRS_LOCK_ADD}",
        "fix: restore the marker",
    )

    assert "Both parents agreed:" not in report(repo, base, head)


def test_a_package_name_that_could_break_its_span_is_counted_not_quoted(repo: Path):
    """A lockfile key is PR-controlled text. A name carrying a backtick would
    close its inline-code span and forge an annotation the reviewer trusts."""
    evil = '[[package]]\nname = "a`b"\nversion = "1.0.0"\ndependencies = ["x"]\n'
    base = commit(repo, "uv.lock", evil, "base")
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "uv.lock", f"{evil}\n{_THEIRS_LOCK_ADD}", "side")
    git(repo, "checkout", "-q", "main")
    commit(repo, "uv.lock", f"{evil}\n{_OURS_LOCK_ADD}", "main")
    subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-edit", "side"],
        capture_output=True,
        text=True,
        check=False,
    )
    head = _resolve_lock_as(
        repo,
        '[[package]]\nname = "a`b"\nversion = "1.0.0"\ndependencies = []\n'
        f"\n{_OURS_LOCK_ADD}\n{_THEIRS_LOCK_ADD}",
    )

    out = report(repo, base, head)
    assert "Both parents agreed:" in out
    assert "cannot quote safely" in out
    assert "`a`b`" not in out


# ── the two new predicates, driven directly ──────────────────────────────────
# The `report()` cases above reach these through a real merge, which can only
# produce the shapes a merge produces. `changed_shared_entries` RETIRES nothing
# but sends a reviewer to a named package, and `forced_collisions` tells one why
# a removal was forced, so what matters for both is every shape they must
# REFUSE — and most of those a git merge cannot be made to write.
_TOP_LEVEL = 'X = 1\n\n\n@deco\ndef a():\n    return "a"\n\n\nclass C:\n    pass\n'


def test_a_definition_segment_starts_at_its_decorator():
    """The survivor comparison reads whole definitions, so a segment that began
    at the `def` would call two decorated copies equal."""
    found = _novelty()._top_level_definitions(_TOP_LEVEL)
    assert found["a"] == ['@deco\ndef a():\n    return "a"']
    assert set(found) == {"a", "C"}


def test_a_file_that_does_not_parse_names_no_definition():
    assert _novelty()._top_level_definitions("def (:\n") is None


_BASE_DUP = "def dup():\n    return 0\n"


@pytest.mark.parametrize(
    ("merged", "base", "ours", "theirs", "expected"),
    [
        pytest.param(
            OURS_DUP, "", OURS_DUP, THEIRS_DUP, ["dup"], id="survivor-is-ours"
        ),
        pytest.param(
            THEIRS_DUP, "", OURS_DUP, THEIRS_DUP, ["dup"], id="survivor-is-theirs"
        ),
        pytest.param(
            OURS_DUP,
            "",
            OURS_DUP,
            OURS_DUP,
            ["dup"],
            id="both-parents-added-the-same-definition",
        ),
        pytest.param(
            OURS_DUP,
            _BASE_DUP,
            OURS_DUP,
            THEIRS_DUP,
            [],
            id="the-base-already-binds-it-so-both-parents-EDITED",
        ),
        pytest.param(
            'def dup():\n    return "new"\n',
            "",
            OURS_DUP,
            THEIRS_DUP,
            [],
            id="survivor-matches-neither-parent",
        ),
        pytest.param(
            OURS_DUP, "", OURS_DUP, KEEP, [], id="only-one-parent-binds-the-name"
        ),
        pytest.param(
            f"{OURS_DUP}\n\n{THEIRS_DUP}",
            "",
            OURS_DUP,
            THEIRS_DUP,
            [],
            id="merged-file-still-binds-it-twice",
        ),
        pytest.param(
            OURS_DUP,
            "",
            f"{OURS_DUP}\n\n{THEIRS_DUP}",
            THEIRS_DUP,
            [],
            id="a-parent-binds-it-twice",
        ),
        pytest.param("def (:\n", "", OURS_DUP, THEIRS_DUP, [], id="unparseable"),
    ],
)
def test_forced_collisions_refuses_every_ambiguity(
    merged, base, ours, theirs, expected
):
    """A wrong name tells the reviewer a real deletion was forced, so the base
    must not bind it, the merged file must bind it once, each parent once, and
    the survivor must be one parent's own bytes."""
    m = _novelty()
    assert m.forced_collisions(merged, m.ParentBlobs(base, ours, theirs)) == expected


_NPM_LOCK = '{"packages": {"a": {"version": "1"}, "b": {"version": "2"}}}'
_NPM_SCOPED = '{"packages": {"node_modules/@scope/bar": {"version": "1"}}}'
_NPM_V1 = '{"dependencies": {"a": {"version": "1"}}}'


@pytest.mark.parametrize(
    ("merged", "ours", "theirs", "path", "expected"),
    [
        pytest.param(
            _RELOCKED,
            _SHARED_LOCK,
            _SHARED_LOCK,
            "uv.lock",
            ["zipfile-zstd"],
            id="toml-entry-both-parents-shared",
        ),
        pytest.param(
            _RELOCKED,
            _SHARED_LOCK,
            _RELOCKED,
            "uv.lock",
            [],
            id="the-parents-disagree-so-the-merge-chose",
        ),
        pytest.param(
            '{"packages": {"a": {"version": "9"}}}',
            _NPM_LOCK,
            _NPM_LOCK,
            "package-lock.json",
            ["a", "b"],
            id="json-entries-the-merge-changed-and-dropped",
        ),
        pytest.param(
            _NPM_V1, _NPM_V1, _NPM_V1, "package-lock.json", [], id="json-v1-table"
        ),
        pytest.param("{", _NPM_LOCK, _NPM_LOCK, "package-lock.json", [], id="bad-json"),
        pytest.param(
            "[]",
            _NPM_LOCK,
            _NPM_LOCK,
            "package-lock.json",
            [],
            id="json-that-is-not-an-object",
        ),
        pytest.param(
            '{"packages": {"node_modules/@scope/bar": {"version": "2"}}}',
            _NPM_SCOPED,
            _NPM_SCOPED,
            "package-lock.json",
            ["node_modules/@scope/bar"],
            id="an-npm-key-carrying-a-slash-and-an-at-sign",
        ),
        pytest.param(
            '{"other": 1}',
            _NPM_LOCK,
            _NPM_LOCK,
            "package-lock.json",
            [],
            id="json-with-no-package-table",
        ),
        pytest.param("[[", _SHARED_LOCK, _SHARED_LOCK, "uv.lock", [], id="bad-toml"),
        pytest.param(
            'package = "x"',
            _SHARED_LOCK,
            _SHARED_LOCK,
            "uv.lock",
            [],
            id="toml-package-is-not-a-list",
        ),
        pytest.param(
            '[[package]]\nversion = "1"\n',
            _SHARED_LOCK,
            _SHARED_LOCK,
            "uv.lock",
            [],
            id="toml-entry-with-no-name",
        ),
        pytest.param(
            f"{_SHARED_LOCK}\n{_SHARED_LOCK}",
            _SHARED_LOCK,
            _SHARED_LOCK,
            "uv.lock",
            [],
            id="one-name-bound-twice",
        ),
        pytest.param(
            _SHARED_LOCK,
            _SHARED_LOCK,
            _SHARED_LOCK,
            "yarn.lock",
            [],
            id="a-format-with-no-parser-here",
        ),
    ],
)
def test_changed_shared_entries_refuses_what_it_cannot_read(
    merged, ours, theirs, path, expected
):
    """A wrong entry name sends a reviewer to the wrong package, so a file this
    cannot parse, a table it does not recognise, and a name bound twice each
    drop the whole file."""
    assert _lockentries().changed_shared_entries(merged, ours, theirs, path) == expected
