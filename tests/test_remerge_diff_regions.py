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

# A generator that writes nothing: a region it leaves alone is not its output.
_IDLE_GENERATOR = "pass\n"

_FILLER = "".join(f"  step{i}: x\n" for i in range(12))


def _ci(hand: str, jobs: str) -> str:
    return (
        f"env:\n  HAND: {hand}\n{_FILLER}"
        "  # BEGIN GENERATED: jobs ci.yaml (gen.py)\n"
        f"  JOBS: '{jobs}'\n"
        "  # END GENERATED: jobs ci.yaml\n"
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
    tmp_path: Path, generator: str, region: str, resolved_generator: str = ""
) -> str:
    """A merge whose ci.yaml conflicts in the hand-written line AND in the
    region, resolved with an invented hand line and REGION as the region body,
    and gen.py rewritten to RESOLVED_GENERATOR when one is given. Returns the
    merge sha."""
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    _write(
        repo,
        {"gen.py": generator, "jobs.txt": "a\n", "ci.yaml": _ci("base", "a")},
        "base",
    )
    git(repo, "checkout", "-q", "-b", "side")
    _write(repo, {"jobs.txt": "c\na\n", "ci.yaml": _ci("THEIRS", "a c")}, "side")
    git(repo, "checkout", "-q", "main")
    _write(repo, {"jobs.txt": "a\nb\n", "ci.yaml": _ci("OURS", "a b")}, "main")
    done = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-edit", "side"],
        capture_output=True,
        check=False,
    )
    assert done.returncode != 0, "the fixture must conflict"
    (repo / "ci.yaml").write_text(_ci("INVENTED", region), encoding="utf-8")
    if resolved_generator:
        (repo / "gen.py").write_text(resolved_generator, encoding="utf-8")
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
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", **env},
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
    sha = _merge(tmp_path, _GENERATOR, "a b c", resolved_generator=forged)
    out = _report(tmp_path, sha, AUTO_RESOLVE_VERIFY_REGENERATED="true")
    assert "**Regenerated region (verified):**" not in out, out


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


@pytest.mark.parametrize(
    ("hunk", "inside"),
    [
        # Deletes the line right after BEGIN (5): inside the region.
        ("@@ -6,1 +5,0 @@\n-  JOBS: 'a'", True),
        # Deletes the hand-written line right after END (8): outside it.
        ("@@ -9,1 +8,0 @@\n-  HAND: x", False),
        # Rewrites the END marker itself.
        ("@@ -8,1 +8,1 @@\n-  # END\n+  # END!", False),
    ],
    ids=["after-begin", "after-end", "marker"],
)
def test_a_hunk_is_inside_only_between_the_markers(hunk: str, inside: bool):
    sys.path.insert(0, str(SCRIPT.parent))
    from _verified_regions import VerifiedRegion, hunk_inside  # noqa: PLC0415

    assert hunk_inside(hunk, [VerifiedRegion("x", "gen.py", 5, 8)]) is inside
