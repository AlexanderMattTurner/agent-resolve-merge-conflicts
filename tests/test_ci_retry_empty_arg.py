"""An empty argument is a caller defect, so the retry loop refuses it once.

`pip install --quiet ''` answers `Invalid requirement: ''` on every attempt, so
retrying it spent five attempts and 30 s, then reported this helper's own
exhaustion message naming pip — and every conflict in that caller went
unresolved. Issue #40, run 32413694701.
"""

import subprocess

from tests._resolver_helpers import REPO_ROOT

LIB = REPO_ROOT / ".github" / "resolver" / "lib-ci-retry.sh"
# covers: .github/resolver/lib-ci-retry.sh


def _drive(
    tmp_path, args: str, *, base_delay: str = "2"
) -> subprocess.CompletedProcess:
    """Source the real library and call `retry` with ARGS, recording each run."""
    marker = tmp_path / "ran"
    script = f"""
set -uo pipefail
source {LIB!s}
export RETRY_BASE_DELAY={base_delay}
# `return`, never `exit`: the loop calls this in the CURRENT shell, so an exit
# would kill the harness before the loop could report anything.
ran() {{ printf 'x' >>{marker!s}; return 1; }}
retry ran {args}
printf 'rc=%s\\n' "$?"
"""
    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    done.marker = marker  # type: ignore[attr-defined]
    return done


def test_an_empty_argument_is_refused_without_running_the_command(tmp_path):
    """Refused BEFORE the first attempt: the command cannot answer differently, so
    running it once only buys its opaque error in place of this one."""
    result = _drive(tmp_path, "''")
    assert "rc=2" in result.stdout
    # Counted over the whole command line, so the command itself is element 1.
    assert "element 2 of that command line is empty" in result.stderr
    # The remedy, not just the diagnosis — the empty element comes from a spec list
    # that was legitimately empty, so the caller must skip the call.
    assert "Skip the call" in result.stderr
    assert not result.marker.exists(), "the command ran despite the refusal"
    # No attempt means no backoff: the loop prints one line per attempt it spends.
    assert "attempt" not in result.stderr


def test_the_position_named_is_the_empty_one(tmp_path):
    """A caller reads this to find which argument it built wrong."""
    result = _drive(tmp_path, "install --quiet ''")
    assert "element 4 of that command line is empty" in result.stderr


def test_a_non_empty_argument_list_still_retries(tmp_path):
    """The other direction, so the refusal cannot harden into "never retry"."""
    result = _drive(tmp_path, "install --quiet pkg", base_delay="0")
    assert "rc=1" in result.stdout
    assert result.marker.read_text(encoding="utf-8") == "x" * 5
    assert "still failing after 5 attempts" in result.stderr
