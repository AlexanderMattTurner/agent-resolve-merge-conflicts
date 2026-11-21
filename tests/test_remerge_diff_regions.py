"""A generated region inside a hand-written file is verified region by region.

A file like a workflow carries hand-written YAML around one `BEGIN GENERATED`
region. Only a WHOLE-file output could be retired as regenerated, so a region
the pre-pass re-derived reached the reviewer as a hand text-merge, and the
self-review refused the push (agent-glovebox#7227). These cases drive real git
merges and a real generator, because the question is what the renderer reads.
"""

import subprocess
import sys
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

# Rewrites the fixture's generated region from jobs.txt, sorted. A person
# writes every other line of that file.
_GENERATOR = """\
import re
from pathlib import Path
jobs = " ".join(sorted(Path("jobs.txt").read_text().split()))
path = Path("ci.yaml")
text = path.read_text()
path.write_text(re.sub(
    r"(# BEGIN GENERATED: jobs ci\\.yaml \\(gen\\.py\\)\\n).*?(^ *# END GENERATED: jobs ci\\.yaml)",
    lambda m: m[1] + f"  JOBS: '{jobs}'\\n" + m[2],
    text, flags=re.S | re.M))
"""

# The same generator with its job list read through a module the tree carries.
_HELPER = (
    'from pathlib import Path\njobs = sorted(Path("jobs.txt").read_text().split())\n'
)
_IMPORTING_GENERATOR = "import helper\n" + _GENERATOR.replace(
    'sorted(Path("jobs.txt").read_text().split())', "helper.jobs"
)

# The same generator, writing four generated lines ahead of the JOBS line. A
# region whose CHANGED line sits below its own start is what a line-number shift
# can cover together with the line after the region.
_PADDED_GENERATOR = _IMPORTING_GENERATOR.replace(
    'f"  JOBS: ', '"  PAD: x\\n" * 4 + f"  JOBS: '
)

# A generator that writes nothing: a region it leaves alone is not its output.
_IDLE_GENERATOR = "pass\n"

_FILLER = "".join(f"  step{i}: x\n" for i in range(12))


_PAD = "  PAD: x\n"


def _ci(
    hand: str, jobs: str | None, odd: str = "", tail: str = "base", pads: int = 0
) -> str:
    """The fixture file. JOBS of None leaves the region's body empty. ODD is a
    hand-written line above the region, for a separator git does not count.
    TAIL is the hand-written line right BELOW the region. PADS are generated
    lines the generator writes AHEAD of the JOBS line, inside the region."""
    body = "" if jobs is None else _PAD * pads + f"  JOBS: '{jobs}'\n"
    return (
        f"env:\n  HAND: {hand}\n{odd}{_FILLER}"
        "  # BEGIN GENERATED: jobs ci.yaml (gen.py)\n"
        f"{body}"
        "  # END GENERATED: jobs ci.yaml\n"
        f"  TAIL: {tail}\n"
    )


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _write(repo: Path, files: dict[str, str], message: str) -> None:
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def _merge(
    tmp_path: Path,
    generator: str,
    region: str | None,
    written: dict[str, str] | None = None,
    links: dict[str, str] | None = None,
    odd: str = "",
    tail: str = "base",
    pads: int = 0,
) -> str:
    """A merge whose ci.yaml conflicts in the hand-written line AND in the
    region, resolved with an invented hand line and REGION as the region body,
    plus every file in WRITTEN written and every symlink in LINKS made by the
    resolution. ODD goes above the region in every revision, so it is no part of
    the delta. TAIL is the resolution's own line below the region, which no
    parent wrote. The base carries helper.py for a generator that imports it.
    Returns the merge sha."""
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    _write(
        repo,
        {
            "gen.py": generator,
            "helper.py": _HELPER,
            "jobs.txt": "a\n",
            "ci.yaml": _ci("base", "a", odd, pads=pads),
        },
        "base",
    )
    git(repo, "checkout", "-q", "-b", "side")
    _write(
        repo,
        {"jobs.txt": "c\na\n", "ci.yaml": _ci("THEIRS", "a c", odd, pads=pads)},
        "side",
    )
    git(repo, "checkout", "-q", "main")
    _write(
        repo,
        {"jobs.txt": "a\nb\n", "ci.yaml": _ci("OURS", "a b", odd, pads=pads)},
        "main",
    )
    done = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-edit", "side"],
        capture_output=True,
        check=False,
    )
    assert done.returncode != 0, "the fixture must conflict"
    (repo / "ci.yaml").write_text(
        _ci("INVENTED", region, odd, tail, pads), encoding="utf-8"
    )
    for name, text in (written or {}).items():
        (repo / name).write_text(text, encoding="utf-8")
    for name, target in (links or {}).items():
        (repo / name).symlink_to(target)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    return git(repo, "rev-parse", "HEAD").strip()


def _report(tmp_path: Path, sha: str, **env: str) -> str:
    return subprocess.run(
        ["python3", str(SCRIPT), "--commit", sha],
        cwd=tmp_path / "r",
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            **env,
        },
    ).stdout


def test_a_regenerated_region_retires_and_the_hand_hunk_stays(tmp_path: Path):
    sha = _merge(tmp_path, _GENERATOR, "a b c")
    out = _report(tmp_path, sha, AUTO_RESOLVE_VERIFY_REGENERATED="true")
    assert "**Regenerated region (verified):**" in out, out
    assert "+  JOBS: 'a b c'" not in out, "a verified region reached the reviewer"
    assert "INVENTED" in out, "the hand-written hunk beside it must still be read"


def test_a_resolution_that_wrote_python_verifies_no_region(tmp_path: Path):
    # The rewritten generator prints the committed body whatever jobs.txt says.
    forged = _GENERATOR.replace(
        'sorted(Path("jobs.txt").read_text().split())', "['a', 'b', 'c']"
    )
    assert forged != _GENERATOR
    sha = _merge(tmp_path, _GENERATOR, "a b c", written={"gen.py": forged})
    out = _report(tmp_path, sha, AUTO_RESOLVE_VERIFY_REGENERATED="true")
    assert "**Regenerated region (verified):**" not in out, out


@pytest.mark.parametrize(
    ("written", "verified"),
    [
        # Python the generator never loads cannot have produced the region.
        ({"notes.py": "x = 1\n"}, True),
        # A forged module the generator imports prints the committed body.
        ({"helper.py": "jobs = ['a', 'b', 'c']\n"}, False),
        # The guard cannot see a native module load, so any one blocks every region.
        ({"notes.so": "x\n"}, False),
    ],
    ids=["unrelated-python", "imported-python", "native-module"],
)
def test_written_python_blocks_only_the_generator_that_loads_it(
    tmp_path: Path, written: dict[str, str], verified: bool
):
    sha = _merge(tmp_path, _IMPORTING_GENERATOR, "a b c", written=written)
    out = _report(tmp_path, sha, AUTO_RESOLVE_VERIFY_REGENERATED="true")
    assert ("**Regenerated region (verified):**" in out) is verified, out


@pytest.mark.parametrize(
    "generator",
    [
        _GENERATOR + "import subprocess\nsubprocess.run(['true'], check=True)\n",
        "import multiprocessing\n" + _GENERATOR,
    ],
    ids=["subprocess", "multiprocessing"],
)
def test_a_generator_whose_children_escape_the_hook_verifies_nothing(
    tmp_path: Path, generator: str
):
    # A child process, or a spawn-context worker, imports without the hook.
    sha = _merge(tmp_path, generator, "a b c", written={"notes.py": "x = 1\n"})
    out = _report(tmp_path, sha, AUTO_RESOLVE_VERIFY_REGENERATED="true")
    assert "**Regenerated region (verified):**" not in out, out


def _trusted(tmp_path: Path, files: dict[str, str]) -> str:
    """The commit a review job trusts: the fixture's root, with FILES changed."""
    repo = tmp_path / "r"
    root = git(repo, "rev-list", "--max-parents=0", "HEAD").strip()
    if not files:
        return root
    git(repo, "checkout", "-q", "-b", "trusted", root)
    _write(repo, files, "trusted")
    sha = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "checkout", "-q", "main")
    return sha


_GIT_CALLING = (
    _IMPORTING_GENERATOR
    + "import subprocess\nsubprocess.run(['git', 'rev-parse', 'HEAD'], check=True)\n"
)
_TRUE_CALLING = (
    _IMPORTING_GENERATOR + "import subprocess\nsubprocess.run(['true'], check=True)\n"
)


# Reads the region body back from the worktree's HEAD instead of deriving it.
_GIT_READING = r"""
import re, subprocess
from pathlib import Path
head = subprocess.run(["git", "show", "HEAD:ci.yaml"], capture_output=True,
                      text=True, check=True).stdout
found = re.search(r"JOBS: '([^']*)'", head)
path = Path("ci.yaml")
text = path.read_text()
path.write_text(re.sub(
    r"(# BEGIN GENERATED: jobs ci\.yaml \(gen\.py\)\n).*?(^ *# END GENERATED: jobs ci\.yaml)",
    lambda m: m[1] + (f"  JOBS: '{found[1]}'" "\n" if found else "") + m[2],
    text, flags=re.S | re.M))
"""


@pytest.mark.parametrize(
    ("kwargs", "verified"),
    [
        # A symlink is neither Python nor native, so the taint set misses it: a
        # generator writing a whole file could write through it, out of the tree.
        ({"links": {"out.mjs": "/tmp/anywhere.mjs"}}, False),
        # An attribute file drives the checkout, so a trusted blob can land as
        # different bytes on disk.
        ({"written": {".gitattributes": "* text\n"}}, False),
        # The same merge with none of the three verifies, so each case above
        # fails for its own reason.
        ({}, True),
    ],
    ids=["symlink", "gitattributes", "plain"],
)
def test_a_tree_that_breaks_the_pin_verifies_nothing(
    tmp_path: Path, kwargs: dict, verified: bool
):
    sha = _merge(tmp_path, _IMPORTING_GENERATOR, "a b c", **kwargs)
    out = _report(
        tmp_path,
        sha,
        AUTO_RESOLVE_VERIFY_REGIONS_TRUSTED_REV=_trusted(tmp_path, {}),
    )
    assert ("**Regenerated region (verified):**" in out) is verified, out


def test_a_separator_git_does_not_count_cannot_swallow_the_hand_line(tmp_path: Path):
    """A line ABOVE the region carrying two U+2028 shifts the region's marker
    lines by two: `str.splitlines` counts them and git's hunk numbers do not.
    `hunk_inside` reads both, so the shifted region covers the resolution's own
    TAIL line, and `_drop_hunks` then hides that line from the reviewer. The
    four PADS put the region's own changed line inside the shifted window too,
    which is what makes the whole hunk eligible to drop."""
    sha = _merge(
        tmp_path,
        _PADDED_GENERATOR,
        "a b c",
        odd="  ODD: a\u2028b\u2028c\n",
        tail="INVENTED-TAIL",
        pads=4,
    )
    out = _report(
        tmp_path, sha, AUTO_RESOLVE_VERIFY_REGIONS_TRUSTED_REV=_trusted(tmp_path, {})
    )
    assert "INVENTED-TAIL" in out, out


def test_a_generator_reading_the_region_from_git_verifies_nothing(tmp_path: Path):
    # Every region is emptied AND committed, so HEAD cannot hand back the body
    # the generator has to re-derive.
    sha = _merge(tmp_path, _GIT_READING, "a b c")
    out = _report(
        tmp_path, sha, AUTO_RESOLVE_VERIFY_REGIONS_TRUSTED_REV=_trusted(tmp_path, {})
    )
    assert "**Regenerated region (verified):**" not in out, out


@pytest.mark.parametrize(
    ("generator", "trusted", "path", "verified"),
    [
        # The trusted commit carries this generator, its import and its marker.
        (_GIT_CALLING, {}, "", True),
        # A generator whose bytes differ from the trusted commit's runs nothing.
        (_IMPORTING_GENERATOR, {"gen.py": _GENERATOR}, "", False),
        # So does one whose import differs, although no resolution wrote it.
        (_IMPORTING_GENERATOR, {"helper.py": "jobs = ['a']\n"}, "", False),
        # A marker the trusted commit lacks names no generator it vouches for.
        (_IMPORTING_GENERATOR, {"ci.yaml": "env: {}\n"}, "", False),
        # A child other than git could load any file in the tree.
        (_TRUE_CALLING, {}, "", False),
        # A relative PATH entry could resolve `git` inside the tree.
        (_GIT_CALLING, {}, ":.", False),
    ],
    ids=[
        "trusted",
        "generator-differs",
        "import-differs",
        "undeclared",
        "non-git-child",
        "relative-path",
    ],
)
def test_a_trusted_commit_pins_every_line_the_generator_runs(
    tmp_path: Path, generator: str, trusted: dict[str, str], path: str, verified: bool
):
    sha = _merge(tmp_path, generator, "a b c")
    out = _report(
        tmp_path,
        sha,
        PATH=f"/usr/bin:/bin:/usr/local/bin{path}",
        AUTO_RESOLVE_VERIFY_REGIONS_TRUSTED_REV=_trusted(tmp_path, trusted),
    )
    assert ("**Regenerated region (verified):**" in out) is verified, out


@pytest.mark.parametrize(
    ("generator", "region", "env"),
    [
        # Bytes the generator does not produce: a hand wrote them.
        (_GENERATOR, "a c b", {"AUTO_RESOLVE_VERIFY_REGENERATED": "true"}),
        # The generator never writes the region, so leaving it alone proves nothing.
        (_IDLE_GENERATOR, "a b c", {"AUTO_RESOLVE_VERIFY_REGENERATED": "true"}),
        # Nobody opted in to running the tree's generators.
        (_GENERATOR, "a b c", {}),
    ],
    ids=["wrong-bytes", "idle-generator", "not-opted-in"],
)
def test_an_unproven_region_stays_in_the_review(
    tmp_path: Path, generator: str, region: str, env: dict[str, str]
):
    sha = _merge(tmp_path, generator, region)
    out = _report(tmp_path, sha, **env)
    assert "**Regenerated region (verified):**" not in out, out
    assert f"+  JOBS: '{region}'" in out, "an unproven region left the review"


def test_an_emptied_region_stays_in_the_review(tmp_path: Path):
    # An idle generator leaves the same empty body, so the match proves nothing.
    sha = _merge(tmp_path, _IDLE_GENERATOR, None)
    out = _report(tmp_path, sha, AUTO_RESOLVE_VERIFY_REGENERATED="true")
    assert "**Regenerated region (verified):**" not in out, out


@pytest.mark.parametrize(
    ("hunk", "inside"),
    [
        # Deletes the line right after BEGIN (5): inside the region.
        ("@@ -6,1 +5,0 @@\n-  JOBS: 'a'", True),
        # Deletes the hand-written line right after END (8): outside it.
        ("@@ -9,1 +8,0 @@\n-  HAND: x", False),
        # Rewrites the END marker itself.
        ("@@ -8,1 +8,1 @@\n-  # END\n+  # END!", False),
        # One hunk that changes a line inside and the hand line after END.
        ("@@ -6,4 +6,4 @@\n-  a\n+  b\n   c\n   # END\n-  HAND: x\n+  HAND: y", False),
    ],
    ids=["after-begin", "after-end", "marker", "spans-the-end"],
)
def test_a_hunk_is_inside_only_between_the_markers(hunk: str, inside: bool):
    sys.path.insert(0, str(SCRIPT.parent))
    from _verified_regions import VerifiedRegion, hunk_inside  # noqa: PLC0415

    assert hunk_inside(hunk, [VerifiedRegion("x", "gen.py", 5, 8)]) is inside
