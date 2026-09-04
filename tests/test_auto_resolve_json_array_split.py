"""Cutting a wide JSON-array conflict into one block per entry.

agent-glovebox#5644: one list that both branches rewrote from opposite ends is a
single conflict block, so the whole file went to one shard and no shard finished
it inside its wall-clock cap. The same head was handed back five times.

The conflicted text here is written by a real `git merge-file --diff3`, never by
hand: the marker spellings, the label lines and where git chooses to cut are
git's own, and a fixture typed from memory would pin this suite's reading of
them instead.
"""

import json
import subprocess
from pathlib import Path

import pytest

from tests._resolver_helpers import load_script

fanout = load_script(".github/resolver/auto-resolve/fanout.py")
hunks = load_script(".github/resolver/auto-resolve/_conflict_hunks.py")
out_of_conflict = load_script(".github/resolver/auto-resolve/_out_of_conflict.py")
split = load_script(".github/resolver/auto-resolve/_json_array_split.py")


def _entry(name: str, *, backends: bool = False, secret: str = "") -> dict:
    """One live-check entry, in the shape `.github/sbx-live/checks.json` carries."""
    out: dict[str, object] = {"id": name, "script": f"bin/checks/{name}.bash"}
    if backends:
        out["backends"] = ["sbx", "docker"]
    if secret:
        out["secret_vars"] = [secret]
    return out


def _render(entries: list[dict]) -> str:
    return json.dumps(entries, indent=2, ensure_ascii=False) + "\n"


def _conflicted(
    tmp_path: Path, ours: str, base: str, theirs: str, *, diff3: bool = True
) -> str:
    """OURS, BASE and THEIRS merged by git, with git's own conflict markers.

    `git merge-file` exits with the number of conflicts it left, so a zero exit
    means the three inputs merged cleanly and every case below would then run
    over a file with no conflict at all.

    Without DIFF3 this is the text `_out_of_conflict` compares a resolution
    against: it re-derives the merge with `git -c merge.conflictStyle=merge
    merge-tree` from the two parents, so its spans are git's own and never this
    pass's.
    """
    names = {"ours": ours, "base": base, "theirs": theirs}
    for name, text in names.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    done = subprocess.run(
        # cwd-git-ok: `git merge-file` merges the three files named in its argv
        # and reads no repository, so there is none for `-C` to point at.
        [
            "git",
            "merge-file",
            *(["--diff3"] if diff3 else []),
            "-L",
            "ours",
            "-L",
            "base",
            "-L",
            "theirs",
            str(tmp_path / "ours"),
            str(tmp_path / "base"),
            str(tmp_path / "theirs"),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert done.returncode > 0, (
        "git merged these three inputs with no conflict, so there is nothing "
        f"for this case to narrow: {done.stdout}{done.stderr}"
    )
    return (tmp_path / "ours").read_text(encoding="utf-8")


@pytest.fixture(name="wide")
def wide_fixture(tmp_path: Path) -> str:
    """The issue's own shape: the base branch added a field to every entry while
    the head retired two of them, so one list is rewritten from both ends."""
    names = ("alpha", "beta", "gamma", "delta", "epsilon")
    base = [_entry(name) for name in names]
    ours = [_entry("alpha"), _entry("gamma", secret="GB_TOKEN"), _entry("epsilon")]
    theirs = [_entry(name, backends=True) for name in names]
    return _conflicted(tmp_path, _render(ours), _render(base), _render(theirs))


def _taking(text: str, side: int) -> str:
    return hunks.splice(
        text,
        {
            block.ordinal: hunks.side_of(block.text, side)
            for block in hunks.hunks_of(text)
        },
    )


def _whole_entries(text: str) -> bool:
    """Whether TEXT is a run of complete array entries — what a shard needs in
    order to answer its block without reading the rest of the file."""
    try:
        json.loads("[" + text.rstrip().rstrip(",") + "]")
    except json.JSONDecodeError:
        return False
    return True


def test_git_cuts_the_list_across_its_entries(wide: str):
    """The premise. Git aligns on the `},` between two entries, so a block it
    writes starts and ends mid-entry — which is why the cut below is taken over
    the whole file rather than block by block."""
    for block in hunks.hunks_of(wide):
        assert not all(
            _whole_entries(hunks.side_of(block.text, side))
            for side in (hunks.OURS, hunks.THEIRS)
        )


def test_narrowing_cuts_the_list_into_per_entry_blocks(wide: str):
    """More blocks than git wrote, and each one smaller: that is the whole
    trade, since one block is what one shard's wall clock has to cover."""
    narrowed = split.narrow("checks.json", wide)
    assert narrowed is not None
    before = hunks.hunks_of(wide)
    after = hunks.hunks_of(narrowed)
    assert len(after) > len(before)
    assert max(len(block.text.splitlines()) for block in after) < max(
        len(block.text.splitlines()) for block in before
    )
    # Each block is now whole entries, so a shard can answer it on its own.
    for block in after:
        for side in (hunks.OURS, hunks.THEIRS):
            assert _whole_entries(hunks.side_of(block.text, side))


def test_narrowing_keeps_both_sides_byte_for_byte(wide: str):
    """The invariant the cut rests on: whichever side a shard takes, the file it
    leaves is the file the wide block would have left."""
    narrowed = split.narrow("checks.json", wide)
    for side in (hunks.OURS, hunks.THEIRS):
        assert _taking(narrowed, side) == _taking(wide, side)
    # A shard picks per BLOCK, so every mix of the sides has to be a merge of the
    # same three versions too — not only the two pure reconstructions above.
    for entries in _every_mix(narrowed):
        keys = [entry["id"] for entry in entries]
        assert len(keys) == len(set(keys)), f"an entry came out twice: {keys}"


def test_an_entry_both_sides_wrote_alike_leaves_the_conflict(tmp_path: Path):
    """Only the entry that differs stays conflicted. The rest is plain text, so
    no shard is launched for it and no model can rewrite it."""
    base = [_entry(name) for name in ("alpha", "beta", "gamma")]
    ours = [_entry("alpha"), _entry("beta", secret="GB_TOKEN"), _entry("gamma")]
    theirs = [_entry("alpha"), _entry("beta", backends=True), _entry("gamma")]
    wide = _conflicted(tmp_path, _render(ours), _render(base), _render(theirs))
    narrowed = split.narrow("checks.json", wide)
    blocks = hunks.hunks_of(narrowed)
    assert len(blocks) == 1
    assert '"beta"' in blocks[0].text
    assert '"gamma"' not in blocks[0].text


def test_a_narrowed_block_carries_only_its_own_ancestor_entry(tmp_path: Path):
    """The `|||||||` section is the one part no side reconstruction covers, so a
    mis-keyed filter would hand the shard another entry's ancestor unseen."""
    base = [_entry(name) for name in ("alpha", "beta", "gamma")]
    ours = [_entry("alpha"), _entry("beta", secret="GB_TOKEN"), _entry("gamma")]
    theirs = [_entry("alpha"), _entry("beta", backends=True), _entry("gamma")]
    wide = _conflicted(tmp_path, _render(ours), _render(base), _render(theirs))
    narrowed = split.narrow("checks.json", wide)
    ancestor = hunks.side_of(hunks.hunks_of(narrowed)[0].text, hunks.BASE)
    assert json.loads("[" + ancestor.rstrip().rstrip(",") + "]") == [_entry("beta")]


def _every_resolution(text: str):
    """The file a fan-out could leave behind, one per mix of side choices,
    sampled where the block count makes every mix too many to walk.

    A shard answers its own block, so the sides are chosen INDEPENDENTLY. That is
    what the two all-ours/all-theirs reconstructions do not cover.
    """
    blocks = hunks.hunks_of(text)
    total = 2 ** len(blocks)
    for choice in range(0, total, max(1, total // 64)):
        yield hunks.splice(
            text,
            {
                block.ordinal: hunks.side_of(
                    block.text, hunks.THEIRS if (choice >> index) & 1 else hunks.OURS
                )
                for index, block in enumerate(blocks)
            },
        )


def _every_mix(text: str):
    """`_every_resolution`'s files, read as the entry lists they are."""
    return (json.loads(resolved) for resolved in _every_resolution(text))


def test_a_reordered_entry_is_never_duplicated_or_dropped(tmp_path: Path):
    """Ours orders the ids a,b,c and theirs b,a,c, so `SequenceMatcher` writes
    the move as a separate insert and a separate delete. Taking theirs at one and
    ours at the other emits `b` twice, and the opposite mix drops it — both are
    valid JSON, so nothing downstream notices. Nothing here can say the two are
    one entry, so the array is refused and git's own blocks stand."""
    base = [_entry(name) for name in ("a", "b", "c")]
    ours = [_entry(name, secret="GB_TOKEN") for name in ("a", "b", "c")]
    theirs = [_entry(name, backends=True) for name in ("b", "a", "c")]
    wide = _conflicted(tmp_path, _render(ours), _render(base), _render(theirs))
    assert split.narrow("checks.json", wide) is None


def test_an_entry_both_sides_renamed_keeps_its_ancestor(tmp_path: Path):
    """Base calls the entry `b`, ours renamed it to `x` and theirs to `y`. The
    two renames group together, but `b` is in neither side's key set, so nothing
    can say the group is about it — and the shard reads an empty `|||||||`
    section as `b` having existed on no branch at all."""
    base = [_entry(name) for name in ("alpha", "b", "gamma")]
    ours = [_entry("alpha"), _entry("x"), _entry("gamma")]
    theirs = [_entry("alpha"), _entry("y"), _entry("gamma")]
    wide = _conflicted(tmp_path, _render(ours), _render(base), _render(theirs))
    narrowed = split.narrow("checks.json", wide)
    for block in hunks.hunks_of(wide if narrowed is None else narrowed):
        ancestor = hunks.side_of(block.text, hunks.BASE)
        assert ancestor.strip(), f"the ancestor of {block.text} came out empty"


def test_one_array_cannot_take_the_whole_fan_out_budget(tmp_path: Path):
    """Each block is one paid shard out of a budget shared with every other file,
    so a long array is cut into at most `MAX_BLOCKS` of them."""
    names = [f"check{index:02d}" for index in range(40)]
    base = [_entry(name) for name in names]
    ours = [_entry(name, secret="GB_TOKEN") for name in names]
    theirs = [_entry(name, backends=True) for name in names]
    wide = _conflicted(tmp_path, _render(ours), _render(base), _render(theirs))
    narrowed = split.narrow("checks.json", wide)
    assert narrowed is not None
    blocks = hunks.hunks_of(narrowed)
    assert 1 < len(blocks) <= split.MAX_BLOCKS < len(names)
    # Merging neighbouring groups back must not change what either side resolves
    # to, and must leave every mix a valid array of all 40 entries.
    for side in (hunks.OURS, hunks.THEIRS):
        assert _taking(narrowed, side) == _taking(wide, side)
    for entries in _every_mix(narrowed):
        assert [entry["id"] for entry in entries] == names


def test_no_resolution_reads_as_an_edit_to_untouched_context(tmp_path: Path):
    """`_out_of_conflict` reverts, or reports, any line a resolution changed
    outside a conflict span — and it takes those spans from git's own merge of
    the two parents, never from the file this pass rewrote.

    Git aligns on the `},` between two entries, so the line that OPENS an entry
    can sit outside the block git cut while the entry sits inside it. A re-cut
    that swallows that line makes a correct per-entry resolution read as an edit
    to untouched context: the revert is then ambiguous, the run lands with
    auto-merge off and a person reads the delta — the handoff this pass exists
    to remove.
    """
    base = [_entry("beta"), _entry("gamma")]
    ours = [_entry("gamma", secret="GB_TOKEN"), _entry("delta")]
    theirs = [_entry("beta", backends=True), _entry("gamma", backends=True)]
    rendered = (_render(ours), _render(base), _render(theirs))
    wide = _conflicted(tmp_path, *rendered)
    mechanical = _conflicted(tmp_path, *rendered, diff3=False)
    narrowed = split.narrow("checks.json", wide)
    # Git's own blocks are clean here, so any violation below is one the re-cut
    # introduced rather than one this shape already had.
    for text in (wide, wide if narrowed is None else narrowed):
        for resolved in _every_resolution(text):
            assert not out_of_conflict.out_of_conflict_hunks(mechanical, resolved)


def test_a_sidecar_path_is_never_re_cut_in_place(tmp_path: Path, wide: str):
    """A sidecar path is the one class this run resolves without writing the
    conflicted file: `install_resolutions` sends its splice to a scratch path
    instead. The re-cut writes the worktree, so it has to skip that class."""
    path = tmp_path / "settings.json"
    path.write_text(wide, encoding="utf-8")
    plan = fanout.Fanout()
    plan.files = [str(path)]
    plan.sidecar = {str(path)}
    plan.plan_work()
    assert path.read_text(encoding="utf-8") == wide


def test_a_shard_is_planned_for_each_narrowed_block(tmp_path: Path, wide: str):
    """What the run actually buys: the fan-out's own plan, over a real file.

    One `Work` entry is one `claude` process with one `SHARD_TIMEOUT_SECONDS`
    budget, so the count IS how the wide list stops being one shard's problem.
    """
    path = tmp_path / "checks.json"
    path.write_text(wide, encoding="utf-8")
    plan = fanout.Fanout()
    plan.files = [str(path)]
    plan.plan_work()
    assert len(plan.work) > len(hunks.hunks_of(wide))
    assert all(work.hunk is not None for work in plan.work)
    # The plan reads the file the shards will read, so the cut is on disk.
    assert len(hunks.hunks_of(path.read_text(encoding="utf-8"))) == len(plan.work)


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("checks.yaml", "a suffix no JSON parser owns"),
        ("checks.json", "a JSON file whose conflict is not an array of objects"),
    ],
)
def test_narrowing_refuses_what_it_cannot_align(tmp_path: Path, path: str, reason: str):
    """Both refusals answer None, which leaves today's whole-block behaviour."""
    base = {"checks": {"alpha": 1}}
    ours = {"checks": {"alpha": 2}}
    theirs = {"checks": {"alpha": 3}}
    wide = _conflicted(
        tmp_path,
        json.dumps(ours, indent=2) + "\n",
        json.dumps(base, indent=2) + "\n",
        json.dumps(theirs, indent=2) + "\n",
    )
    assert split.narrow(path, wide) is None, reason


def test_a_file_carrying_more_than_the_array_is_left_alone(tmp_path: Path):
    """The parser reads the array and says where it ends, so text after the `]`
    refuses the cut.

    Scanning for the last `]` instead accepts this: the trailing line rides in
    the suffix, the re-cut round-trips, and the file is re-emitted as though it
    were the array alone."""
    base = [_entry(name) for name in ("alpha", "beta")]
    ours = [_entry("alpha"), _entry("beta", secret="GB_TOKEN")]
    theirs = [_entry("alpha"), _entry("beta", backends=True)]
    trailing = "not json\n"
    wide = _conflicted(
        tmp_path,
        _render(ours) + trailing,
        _render(base) + trailing,
        _render(theirs) + trailing,
    )
    assert split.narrow("checks.json", wide) is None


def test_entries_with_no_shared_string_key_are_left_alone(tmp_path: Path):
    """Nothing here can say which entry answers which without a key, so the wide
    block stands rather than being cut on position."""
    base = [{"port": 1}, {"port": 2}, {"port": 3}]
    ours = [{"port": 1}, {"port": 9}, {"port": 3}]
    theirs = [{"port": 1}, {"port": 8}, {"port": 3}, {"port": 4}]
    wide = _conflicted(tmp_path, _render(ours), _render(base), _render(theirs))
    assert split.narrow("checks.json", wide) is None


def test_a_block_git_wrote_without_a_base_section_still_cuts(tmp_path: Path):
    """`merge.conflictStyle` is a repository setting, so the two-side block is a
    shape this must read as well as the diff3 one."""
    base = [_entry(name) for name in ("alpha", "beta", "gamma")]
    ours = [_entry("alpha"), _entry("beta", secret="GB_TOKEN"), _entry("gamma")]
    theirs = [_entry(name, backends=True) for name in ("alpha", "beta", "gamma")]
    for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
        (tmp_path / name).write_text(_render(text), encoding="utf-8")
    done = subprocess.run(
        # cwd-git-ok: `git merge-file` merges the three files named in its argv
        # and reads no repository, so there is none for `-C` to point at.
        [
            "git",
            "merge-file",
            "-L",
            "ours",
            "-L",
            "theirs",
            str(tmp_path / "ours"),
            str(tmp_path / "base"),
            str(tmp_path / "theirs"),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert done.returncode > 0, f"{done.stdout}{done.stderr}"
    wide = (tmp_path / "ours").read_text(encoding="utf-8")
    assert hunks.sides_of(hunks.hunks_of(wide)[0].text).base is None
    narrowed = split.narrow("checks.json", wide)
    assert narrowed is not None
    for side in (hunks.OURS, hunks.THEIRS):
        assert _taking(narrowed, side) == _taking(wide, side)


def test_two_entries_on_one_line_are_not_cut(tmp_path: Path):
    """A cut has to land on a line boundary — git's markers are line-oriented —
    so a compact array is left whole."""
    base = "[{'id': 'a'}, {'id': 'b'}]\n".replace("'", '"')
    ours = "[{'id': 'a'}, {'id': 'c'}]\n".replace("'", '"')
    theirs = "[{'id': 'a'}, {'id': 'd'}]\n".replace("'", '"')
    wide = _conflicted(tmp_path, ours, base, theirs)
    assert split.narrow("checks.json", wide) is None


def test_a_block_whose_markers_do_not_pair_is_refused():
    """`sides_of` answers None rather than guessing where a side ends."""
    assert hunks.sides_of("<<<<<<< ours\nalpha\n") is None
    assert hunks.sides_of("plain\n") is None
    assert hunks.sides_of("<<<<<<< ours\na\n>>>>>>> theirs\nafter\n") is None
    # A criss-cross merge writes its own ancestor's markers into the base
    # section. They delimit nothing this can hand back, so the block is refused.
    assert (
        hunks.sides_of(
            "<<<<<<< ours\na\n||||||| base\n<<<<<<< x\nb\n>>>>>>> y\n"
            "=======\nc\n>>>>>>> theirs\n"
        )
        is None
    )


def test_the_tail_a_side_ends_early_stays_in_one_block(tmp_path: Path):
    """An array's last entry is the one with no comma after it. A group that
    ends one side's list has to carry the other side's tail with it, or a shard
    taking the short side leaves `}` and `{` with nothing between them."""
    base = [_entry(name) for name in ("alpha", "beta")]
    ours = [_entry(name, secret="GB_TOKEN") for name in ("alpha", "beta")]
    theirs = [_entry(name, backends=True) for name in ("alpha", "beta", "gamma")]
    wide = _conflicted(tmp_path, _render(ours), _render(base), _render(theirs))
    narrowed = split.narrow("checks.json", wide)
    assert narrowed is not None
    blocks = hunks.hunks_of(narrowed)
    # Every mix of the sides is still an array, which is what the fan-out's own
    # separability check asks of a file before it cuts a shard per block.
    for choice in range(2 ** len(blocks)):
        picked = {
            block.ordinal: hunks.side_of(
                block.text, hunks.THEIRS if (choice >> index) & 1 else hunks.OURS
            )
            for index, block in enumerate(blocks)
        }
        json.loads(hunks.splice(narrowed, picked))


def _conflict(ours: str, theirs: str) -> str:
    """A two-sided conflict block, written here rather than by git: the cases
    below need JSON git's own merge of two valid files cannot produce."""
    return f"<<<<<<< ours\n{ours}=======\n{theirs}>>>>>>> theirs\n"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ('[\n  {"id": "a"}\n]\n', "no conflict at all, so nothing to narrow"),
        (
            "[\n" + _conflict('  "a"\n', '  "b"\n') + "]\n",
            "array members that are not objects",
        ),
        (
            "[\n"
            + _conflict('  {"id": "a"}, {"id": "b"}\n', '  {"id": "c"}\n')
            + "]\n",
            "two entries sharing one line, which no marker can be cut between",
        ),
        (
            "[\n" + _conflict('  {"id": "a"} junk\n', '  {"id": "b"}\n') + "]\n",
            "a side that is not JSON at all",
        ),
        (
            "[\n" + _conflict('  {"id": "a"}]\n', '  {"id": "b"}]\n'),
            "a closing bracket sharing a line with an entry",
        ),
        (
            "[\n"
            + _conflict('  {"id": "a", "n": 1}\n', '  {"id": "a", "n": 2}\n')
            + "]\n",
            "one entry, so the cut would be the block it started from",
        ),
        (
            "[\n" + _conflict('  {"id": "a"}\n', '  {"id": "b"}\n'),
            "an array the file never closes",
        ),
        (
            '[\n<<<<<<< ours\n  {"id": "a"}\n>>>>>>> theirs\n]\n',
            "a block with no separator, so no side can be read off it",
        ),
    ],
)
def test_narrow_refuses_and_leaves_the_text_alone(text: str, reason: str):
    assert split.narrow("checks.json", text) is None, reason


def test_a_cut_that_would_change_the_merge_is_thrown_away(
    wide: str, monkeypatch, capsys
):
    """The safety net. Whatever the alignment produced, a re-cut that does not
    resolve to the same bytes on both sides is discarded and said out loud."""
    monkeypatch.setattr(split, "_emit", lambda *_: '[\n  {"id": "wrong"}\n]\n')
    assert split.narrow("checks.json", wide) is None
    assert "::warning::" in capsys.readouterr().out
