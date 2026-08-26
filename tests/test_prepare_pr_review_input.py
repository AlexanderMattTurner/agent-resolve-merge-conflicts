"""Behavioral tests for .github/scripts/prepare-pr-review-input.sh — the step
that fetches the untrusted PR diff/metadata and runs them through the
sanitizer before the review agent sees them.

Contract:
  * At or under MAX_DIFF_LINES: oversized=false, diff.txt/meta.txt written.
  * Over MAX_DIFF_LINES: oversized=true, oversized-notice.txt written, and
    diff.txt/meta.txt are NOT written (the review is skipped for size).
  * A diff GitHub itself refuses to serve (HTTP 406 over its own 20000-line cap)
    reaches the SAME skip, without spending the retry ladder on a deterministic
    refusal. The REST diff media type serves past that cap, so the line count —
    not the refusal — is the live guard for an oversized diff.
  * A diff holding a raw terminal escape byte still reaches the sanitizer. That
    is why the fetch is curl and not `gh pr diff`, whose client-side guard
    refuses to print such a response (observed on agent-sanitizer#320).
  * The generated-file filter runs BEFORE the line count, so a small source edit
    that also rebuilds a committed artifact is reviewed instead of skipped.

The tests drive the REAL script with a fake `curl` (emits an N-file unified
diff), a fake `gh` (PR metadata), and a fake `node` (stands in for the sanitizer,
passing stdin through) on PATH. The fake `curl` produces the FAULT the real API
cannot be made to produce here — a 406 over its own line cap — and never stands
in for its ordinary work beyond the diff bytes.
"""

import re
import shutil
import subprocess
from pathlib import Path

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "scripts" / "prepare-pr-review-input.sh"

# Each fake file section is this many lines (header + ---/+++ + @@ + one body
# line), so a diff's line count is a simple multiple of its file count.
LINES_PER_FILE = 5

# A refusal body in the API's own words, taken from the 406 `gh pr diff` wrapped
# on a real over-cap PR here. The REST diff media type has NOT been seen to
# refuse — its evidence is a successful 31,204-line fetch — so this pins a
# defensive path, and the classification is gated on curl's HTTP-error status.
# The substring prepare-pr-review-input.sh classifies a refusal on, READ FROM the
# script: a copy would drift and quietly stop putting the real marker in the PR's
# own diff, leaving the test that proves content cannot classify itself vacuous.
API_OVERSIZE_MARKER = re.search(
    r'^API_OVERSIZE_MARKER="(?P<marker>[^"]+)"$',
    SCRIPT.read_text(encoding="utf-8"),
    re.M,
).group("marker")

API_TOO_LARGE_BODY = (
    '{"message":"Sorry, the diff exceeded the maximum number of lines (20000).",'
    '"documentation_url":"https://docs.github.com/rest/pulls/pulls"}'
)


def _fake_bins(
    tmp_path: Path,
    *,
    files: int,
    escape_byte: bool = False,
    diff_too_large: bool = False,
    marker_in_diff: bool = False,
) -> None:
    """Put a fake `gh` and a fake `node` (the sanitizer stand-in: cats stdin) on
    PATH. The fake `gh` emits a `files`-file unified diff for `pr diff` and JSON
    for `pr view`, and refuses `pr diff` without --allow-escape-sequences —
    mirroring the real CLI's guard — so every test also asserts the script
    keeps passing that flag. `escape_byte` adds one hunk holding a literal ESC
    byte, mirroring the payload `gh pr diff` would otherwise refuse to print.
    `diff_too_large` makes `pr diff` fail the way the API does above its own cap,
    and counts the attempts so a test can assert the refusal was not retried.
    """
    marker = ""
    if marker_in_diff:
        marker = (
            '  echo "diff --git a/notes.md b/notes.md"\n'
            '  echo "@@ -0,0 +1,1 @@"\n'
            f'  echo "+{API_OVERSIZE_MARKER}"\n'
        )
    escape = ""
    if escape_byte:
        escape = (
            '  echo "diff --git a/escape.txt b/escape.txt"\n'
            '  echo "@@ -0,0 +1,1 @@"\n'
            '  printf "+escaped \x1b[31mred\x1b[0m line\\n"\n'
        )
    too_large = ""
    if diff_too_large:
        too_large = (
            '  echo "diff" >>"$GH_DIFF_ATTEMPTS"\n'
            # --fail-with-body puts a refusal's BODY on stdout, which is where
            # the marker the script classifies on lives.
            f"  echo '{API_TOO_LARGE_BODY}'\n"
            "  exit 22\n"
        )
    curl = tmp_path / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        # Drain the --config stdin the real call feeds, or printf takes SIGPIPE
        # and pipefail turns a fine fetch into a failure. Record argv so a test
        # can assert the media type the whole change rests on.
        "cat >/dev/null\n"
        'printf "%s\\n" "$@" >>"${SANITIZE_INPUT}.curl_argv"\n'
        f"{too_large}"
        f"for ((i = 0; i < {files}; i++)); do\n"
        '  echo "diff --git a/f$i.py b/f$i.py"\n'
        '  echo "--- a/f$i.py"\n'
        '  echo "+++ b/f$i.py"\n'
        '  echo "@@ -0,0 +1,1 @@"\n'
        '  echo "+added line $i"\n'
        "done\n"
        f"{escape}"
        f"{marker}",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$2" == "view" ]]; then\n'
        '  printf \'%s\' \'{"title":"t","body":"b","author":{"login":"a"},"files":[]}\'\n'
        "fi\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    # The fake `node` stands in for the SANITIZER and for the ownership ORACLE,
    # both of which have their own tests. strip-generated-diff.mjs is the one
    # this script's wiring is about, so it runs for real: a stub there would let
    # the filter be called in the wrong order and still pass.
    real_node = shutil.which("node")
    assert real_node, "node is required to run strip-generated-diff.mjs"
    node = tmp_path / "node"
    node.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  *resolve-generated.mjs)\n"
        '    printf \'%s\' "${OMIT_PATHS:-}"; exit "${ORACLE_EXIT:-0}" ;;\n'
        "  *strip-generated-diff.mjs)\n"
        f'    exec {real_node} "$@" ;;\n'
        "esac\n"
        'tee -a "$SANITIZE_INPUT"\n',
        encoding="utf-8",
    )
    node.chmod(0o755)


def _run(
    tmp_path: Path,
    *,
    files: int,
    max_diff_lines: int,
    escape_byte: bool = False,
    diff_too_large: bool = False,
    marker_in_diff: bool = False,
    omit: tuple[str, ...] = (),
    oracle_exit: int = 0,
) -> tuple[subprocess.CompletedProcess, dict[str, str], Path]:
    _fake_bins(
        tmp_path,
        files=files,
        escape_byte=escape_byte,
        diff_too_large=diff_too_large,
        marker_in_diff=marker_in_diff,
    )
    out_file = tmp_path / "github_output"
    out_file.write_text("", encoding="utf-8")
    input_dir = tmp_path / "pr-input"
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "GITHUB_OUTPUT": str(out_file),
            "SANITIZE_INPUT": str(tmp_path / "sanitizer_input"),
            "GH_TOKEN": "fake",
            "GH_REPO": "owner/repo",
            "PR": "123",
            "PR_INPUT_DIR": str(input_dir),
            "GH_DIFF_ATTEMPTS": str(tmp_path / "gh_diff_attempts"),
            "MAX_DIFF_LINES": str(max_diff_lines),
            # Keeps a regressed (flag-dropped) run's retry ladder off the
            # 2+4+8+16s backoff: a bare assertion failure beats a 30s-per-test
            # wait for a run that is going to fail either way.
            "RETRY_MAX": "1",
            "RETRY_BASE_DELAY": "0",
            "OMIT_PATHS": "".join(f"{p}\n" for p in omit),
            "ORACLE_EXIT": str(oracle_exit),
        },
    )
    outputs = dict(
        ln.split("=", 1)
        for ln in out_file.read_text(encoding="utf-8").splitlines()
        if "=" in ln
    )
    return proc, outputs, input_dir


def test_normal_diff_is_sanitized(tmp_path: Path) -> None:
    proc, outputs, input_dir = _run(tmp_path, files=2, max_diff_lines=100)
    assert proc.returncode == 0, proc.stderr
    assert outputs["oversized"] == "false"
    diff_body = (input_dir / "diff.txt").read_text(encoding="utf-8")
    assert diff_body.count("diff --git ") == 2
    assert "+added line 0" in diff_body and "+added line 1" in diff_body
    assert (input_dir / "meta.txt").is_file()
    assert not (input_dir / "oversized-notice.txt").exists()
    assert (tmp_path / "sanitizer_input").exists(), "the sanitizer must run"
    # The media type is the only reason the fetch serves past GitHub's own cap,
    # and the URL is the only thing aiming it at this PR. Both are invisible to
    # every other assertion here, which a fake that ignores argv would hide.
    argv = (tmp_path / "sanitizer_input.curl_argv").read_text(encoding="utf-8")
    assert "Accept: application/vnd.github.v3.diff" in argv
    assert "/repos/owner/repo/pulls/123" in argv
    assert "Authorization" not in argv, "the token must not ride in argv"


def test_oversized_diff_skips_the_review(tmp_path: Path) -> None:
    proc, outputs, input_dir = _run(tmp_path, files=6, max_diff_lines=10)
    assert proc.returncode == 0, proc.stderr
    assert outputs["oversized"] == "true"
    assert outputs["diff_lines"] == str(6 * LINES_PER_FILE)
    assert (input_dir / "oversized-notice.txt").is_file()
    assert not (input_dir / "diff.txt").exists()
    assert not (input_dir / "meta.txt").exists(), "the size skip must also skip meta"


def test_a_diff_github_refuses_to_serve_takes_the_same_skip(tmp_path: Path) -> None:
    """The line-count guard cannot reach the largest PRs: GitHub answers 406
    before any byte arrives, so without this the graceful notice is dead code and
    the check reds instead. RED if the refusal stops routing to the size skip."""
    proc, outputs, input_dir = _run(
        tmp_path, files=2, max_diff_lines=100, diff_too_large=True
    )
    assert proc.returncode == 0, proc.stderr
    assert outputs["oversized"] == "true"
    assert outputs["diff_lines"] == "20000"
    assert (input_dir / "oversized-notice.txt").is_file()
    assert not (input_dir / "diff.txt").exists()
    assert not (input_dir / "meta.txt").exists()


def test_the_refusal_is_not_retried(tmp_path: Path) -> None:
    """A 406 over the API's own cap is deterministic, so a second fetch buys the
    same answer and the backoff before it. Asserted as a COUNT rather than as
    elapsed time, which a loaded runner makes meaningless."""
    _, _, _ = _run(
        tmp_path,
        files=2,
        max_diff_lines=100,
        diff_too_large=True,
    )
    attempts = (tmp_path / "gh_diff_attempts").read_text(encoding="utf-8").splitlines()
    assert len(attempts) == 1, f"the size refusal was fetched {len(attempts)} times"


def test_a_diff_with_a_raw_escape_byte_still_reaches_the_sanitizer(
    tmp_path: Path,
) -> None:
    """`gh pr diff` refuses to emit a diff holding a raw terminal escape byte, so
    a PR carrying one (observed on agent-sanitizer#320) died before the sanitizer
    ever ran. curl has no such guard, and the bytes reach only the sanitizer,
    never a real terminal. RED if the fetch goes back through gh."""
    proc, outputs, input_dir = _run(
        tmp_path, files=2, max_diff_lines=100, escape_byte=True
    )
    assert proc.returncode == 0, proc.stderr
    assert outputs["oversized"] == "false"
    sanitizer_saw = (tmp_path / "sanitizer_input").read_text(encoding="utf-8")
    assert "\x1b[31m" in sanitizer_saw, "the raw byte must reach the sanitizer intact"


def test_a_generated_file_is_stripped_before_the_size_count(tmp_path: Path) -> None:
    """The whole point of the filter. Six files run to 30 lines, over the 20-line
    cap, so an unfiltered run takes the size skip and the hand-written change gets
    no read. Omitting five leaves one file plus the note, under the cap. RED if
    the filter runs after the count, or not at all."""
    omit = tuple(f"f{i}.py" for i in range(5))
    proc, outputs, input_dir = _run(tmp_path, files=6, max_diff_lines=20, omit=omit)
    assert proc.returncode == 0, proc.stderr
    assert outputs["oversized"] == "false"
    diff_body = (input_dir / "diff.txt").read_text(encoding="utf-8")
    assert "+added line 5" in diff_body, "the hand-written file must survive"
    assert "+added line 0" not in diff_body, "the omitted file must be gone"
    assert "5 generated file(s) are omitted" in diff_body, "the note must say so"


def test_an_empty_omit_list_leaves_the_diff_untouched(tmp_path: Path) -> None:
    """A repository that declares no regen rules gets an empty list, and must see
    the same diff and the same verdict it saw before the filter existed."""
    proc, outputs, input_dir = _run(tmp_path, files=6, max_diff_lines=20)
    assert proc.returncode == 0, proc.stderr
    assert outputs["oversized"] == "true"
    assert outputs["diff_lines"] == str(6 * LINES_PER_FILE)
    assert not (input_dir / "diff.txt").exists()


def test_a_broken_oracle_fails_the_step_rather_than_omitting_nothing(
    tmp_path: Path,
) -> None:
    """The fail-CLOSED half of the posture. An oracle that dies must not degrade
    into an empty omit list: that reads as "nothing is generated", which is the
    same answer a correct empty list gives. RED if the call is ever wrapped in a
    `|| true` or a command substitution that swallows the status."""
    proc, outputs, input_dir = _run(
        tmp_path, files=2, max_diff_lines=100, oracle_exit=1
    )
    assert proc.returncode != 0
    assert not (input_dir / "diff.txt").exists()
    assert "oversized" not in outputs


def test_a_pr_cannot_classify_itself_as_oversized(tmp_path: Path) -> None:
    """The marker is matched only on curl's HTTP-error status. Without that gate
    a PR whose own diff carries the refusal's words — or a partial diff left by a
    transport failure mid-transfer — would skip its security review and go green.
    RED if the status check is dropped from the elif."""
    proc, outputs, input_dir = _run(
        tmp_path, files=1, max_diff_lines=100, marker_in_diff=True
    )
    assert proc.returncode == 0, proc.stderr
    assert outputs["oversized"] == "false", "PR content must never classify"
    assert (input_dir / "diff.txt").is_file(), "the review must still happen"
    assert not (input_dir / "oversized-notice.txt").exists()
