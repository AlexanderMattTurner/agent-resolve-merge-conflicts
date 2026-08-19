"""Real HTTPS GitHub API servers, so tests drive the REAL `gh` binary.

Nothing here fakes `gh`. The tests run `/usr/bin/gh` and the real repo scripts
unmodified; what is simulated is GitHub itself — a localhost HTTPS server holding
mutable state and answering the endpoints the mechanism under test uses. That
boundary is what makes the tests worth running: a wrong request path, a malformed
body, a flag the CLI does not accept, or a response shape gh rejects all surface
as a loud failure, none of which an argv-level `gh` stub can see.

Two mechanics make it work, both observed rather than assumed:

  * gh treats any GH_HOST other than github.com as GitHub Enterprise and talks
    to `https://HOST/api/v3/…`, so the server must serve TLS — plain HTTP is
    refused with "first record does not look like a TLS handshake".
  * gh is a Go program, and Go's x509 loader honours SSL_CERT_FILE, so pointing
    it at a throwaway self-signed cert is enough to be trusted. No verification
    is disabled anywhere.

PROBLEM CLASS — a real-gh fixture that cannot trust its own self-signed CA on
macOS. That second mechanic is LINUX-ONLY: on macOS Go builds its root pool from
the system Security framework and ignores SSL_CERT_FILE, so gh rejects this
server's certificate with `tls: failed to verify certificate: x509: certificate
signed by unknown authority` (observed on the `Cross-platform host tests (macOS,
shard 0)` leg of PR #3239 and the `shard 1` leg of PR #3656). `_LocalGitHub`
therefore SKIPS itself on darwin, in its constructor. That placement is the
enforcement point: any test reaching this transport skips, whether it asked for
the cross-platform marker or inherited it. `tests/conftest.py` derives that
marker per MODULE from the `# covers:` directive, so a module gains it by
covering an unrelated host file — which is how the gh-stack cases in
tests/test_session_setup.py landed on the macOS leg with no edit of their own.

`_LocalGitHub` carries that transport, plus the two things every table would
otherwise restate: `GET /api/v3/meta`, the probe each gh command opens with, and
the 404 that names any path no table modelled — so a script reaching for an
unmodelled endpoint fails loudly instead of reading a plausible empty answer.
Each subclass supplies only its own endpoints, via `resolve`.

`FakeIssueComments` — one PR's issue comments, for the sticky-comment scripts:

  GET    /api/v3/repos/{o}/{r}/issues/{n}/comments      (find the marked comment)
  GET    /api/v3/repos/{o}/{r}/issues/comments/{id}     (read its body)
  PATCH  /api/v3/repos/{o}/{r}/issues/comments/{id}     (rewrite it in place)
  POST   /api/v3/repos/{o}/{r}/issues/{n}/comments      (first run only)

`FakeActionsArtifacts` — the repo-wide artifact listing filtered by name, this
workflow file's id lookup, and the artifact-zip download with its real
302-to-blob redirect, for the resolver's bundle-reuse probe.

`FakeHeadRuns` — one head's workflow runs and the re-run POST.

`FakeResolverGitHub` — what the merge-conflict auto-resolver's discovery reads:

  POST   /api/graphql                                  (pr list, pr view, the
                                                        isInMergeQueue probe)
  GET    /api/v3/repos/{o}/{r}/commits/{sha}/statuses  (the attempt mark)
"""

import functools
import importlib.util
import json
import re
import ssl
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode

import coverage
import pytest
from graphql import parse
from graphql.language import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    OperationDefinitionNode,
    VariableNode,
)

from tests._resolver_helpers import (
    REPO_ROOT,
    current_path,
    load_script,
    run_capture,
)

# The sweep's own shared module, loaded rather than re-derived: a second reading
# of the mergeability field set here would be a copy that drifts from the reading
# the scripts under test make.
_pr_sweep = load_script(".github/resolver/_pr_sweep.py")


# What `add_run` stamps as a run's completion time when the scenario names
# none: long before any burst window a test measures, so a seeded red only
# coalesces with another when the test says so.
OLD_COMPLETION = "2026-01-01T00:00:00Z"

DISCOVER_SCRIPT = REPO_ROOT / ".github" / "resolver" / "auto-resolve" / "discover.py"
MARK_ATTEMPT_SCRIPT = (
    REPO_ROOT / ".github" / "resolver" / "auto-resolve" / "mark-attempt.sh"
)


# What every server here answers `GET /api/v3/meta` with. gh treats a non-github.com
# host as Enterprise and reads this version to decide which query shapes the host
# supports; any value it can parse is enough, because nothing under test branches on
# the number.
GHE_VERSION = "3.14.0"

# GitHub's own body when it refuses a request for the primary rate limit, copied
# from the 403 in run 31638710987. Served rather than hand-typed at each call
# site, so a test cannot assert against a refusal GitHub does not send.
RATE_LIMIT_REFUSAL = (
    "API rate limit exceeded for installation. If you reach out to GitHub "
    "Support for help, please include the request ID."
)

# Why every server here refuses to run on macOS. Registered verbatim in
# .github/scripts/skip-allowlist.json, which reds any skip reason it does not carry.
DARWIN_GH_TLS_SKIP = (
    "the real gh binary cannot trust this fixture's self-signed CA on macOS: "
    "Go builds its root pool from the Security framework and ignores SSL_CERT_FILE"
)


def coverage_env() -> dict[str, str]:
    """The one variable that turns measurement on inside a child interpreter.

    coverage installs a `.pth` file that starts measuring only when
    COVERAGE_PROCESS_START names a config file, and the environment a script runs
    under here is built from scratch rather than inherited. Without this a Python
    script driven as a subprocess reports 0% however thoroughly it is tested, and
    a coverage gate then reds on a file the suite covers. Empty when the parent
    run is not measuring, so an ordinary run writes no data files.

    A scratch-tree `.py` file that poisons the combined coverage
    data. INVARIANT for every caller: a child measured through here may run with a
    scratch directory as its cwd, and pyproject's `source = ["."]` resolves against
    THAT cwd while `relative_files = true` records each path relative to it. So
    every `.py` file in that scratch tree — executed or not, since coverage sweeps
    the source dir for unexecuted files too — enters the data at a repo-relative
    path. Put one at a path the repository does not carry and the gate's
    `coverage report` fails the whole run with "No source for code: <path>".
    `_helpers.copy_script_at_same_path` is what keeps the driven script's own path
    valid; a fixture file the test invents must avoid the `.py` suffix."""
    if coverage.Coverage.current() is None:
        return {}
    return {
        "COVERAGE_PROCESS_START": str(REPO_ROOT / "pyproject.toml"),
        # A child writes its data file into its own working directory, and a test
        # that drives a script inside a scratch repo leaves it there, where
        # `coverage combine` at the repo root never finds it. The file then reads
        # as 0% for a script the suite covers, so the gate reds on the measurement
        # rather than on the code.
        "COVERAGE_FILE": str(REPO_ROOT / ".coverage"),
    }


_COMMIT_RE = re.compile(r"^/api/v3/repos/[^/]+/[^/]+/commits/(?P<sha>[^/]+)$")
_PULL_RE = re.compile(r"^/api/v3/repos/[^/]+/[^/]+/pulls/(?P<pr>\d+)$")
# A branch name carries slashes, so both of these match the REST of the path.
_COMPARE_RE = re.compile(
    r"^/api/v3/repos/[^/]+/[^/]+/compare/(?P<base>[^.]+)\.\.\.(?P<head>.+)$"
)


_STATUSES_RE = re.compile(
    r"^/api/v3/repos/[^/]+/[^/]+/commits/(?P<sha>[^/]+)/statuses$"
)
_BRANCH_RE = re.compile(r"^/api/v3/repos/[^/]+/[^/]+/branches/(?P<branch>[^/]+)$")
_TIMELINE_RE = re.compile(r"^/api/v3/repos/[^/]+/[^/]+/issues/(?P<pr>\d+)/timeline$")
# Older than any date a resolver test states, so the page-one decoys sort before
# every event under test however far back that test backdates one.
_TIMELINE_DECOY_AGE_SECS = 100_000 * 3600
_ISSUE_COMMENTS_RE = re.compile(
    r"^/api/v3/repos/[^/]+/[^/]+/issues/(?P<pr>\d+)/comments$"
)
_STATUS_WRITE_RE = re.compile(r"^/api/v3/repos/[^/]+/[^/]+/statuses/(?P<sha>[^/]+)$")
_PR_COMMITS_RE = re.compile(r"^/api/v3/repos/[^/]+/[^/]+/pulls/(?P<pr>\d+)/commits$")

# GitHub's ceiling on a query's node estimate.
_MAX_NODES = 500_000


def _fragments(document: DocumentNode) -> dict[str, FragmentDefinitionNode]:
    return {
        d.name.value: d
        for d in document.definitions
        if isinstance(d, FragmentDefinitionNode)
    }


def _selections(node, fragments: dict) -> list[FieldNode]:
    """The fields `node` selects, with fragment spreads resolved in place — which
    is where a fragment's connections really nest, and what a text scan of gh's
    query (whose whole projection is one fragment) cannot see."""
    fields: list[FieldNode] = []
    for selection in node.selection_set.selections if node.selection_set else ():
        if isinstance(selection, FieldNode):
            fields.append(selection)
        elif isinstance(selection, FragmentSpreadNode):
            fields.extend(_selections(fragments[selection.name.value], fragments))
        elif isinstance(selection, InlineFragmentNode):
            fields.extend(_selections(selection, fragments))
    return fields


def _operations(document: DocumentNode) -> list[OperationDefinitionNode]:
    return [d for d in document.definitions if isinstance(d, OperationDefinitionNode)]


def _projected_fields(document: DocumentNode, fragments: dict) -> list[str]:
    """The `--json` field names a gh query projects onto a pull request — a
    listing's and a single view's alike, since only the wrapper differs."""

    def walk(node) -> list[str] | None:
        for field in _selections(node, fragments):
            if field.name.value in ("pullRequest", "pullRequests"):
                target = field
                for inner in _selections(field, fragments):
                    if inner.name.value == "nodes":  # a listing's connection
                        target = inner
                return [f.name.value for f in _selections(target, fragments)]
            found = walk(field)
            if found is not None:
                return found
        return None

    for operation in _operations(document):
        found = walk(operation)
        if found is not None:
            return found
    raise AssertionError("fake GitHub: query selects no pull request")


def _projects(document: DocumentNode, fragments: dict, name: str) -> bool:
    """True when the query selects a field called `name` anywhere."""

    def walk(node) -> bool:
        return any(
            field.name.value == name or walk(field)
            for field in _selections(node, fragments)
        )

    return any(walk(operation) for operation in _operations(document))


def _node_estimate(
    document: DocumentNode, fragments: dict, variables: dict
) -> tuple[int, str]:
    """The largest node count a query can return and the connection where it is
    reached — GitHub's own estimate: the product of the `first:` arguments along
    each path of nested connections.

    This is what refuses a PR listing that also asks for `commits` (100 PRs x 100
    commits x 100 authors) while serving the same projection on ONE pull request
    (100 x 100).
    """
    peak = (1, "")

    def page_size(field: FieldNode) -> int:
        for argument in field.arguments:
            if argument.name.value != "first":
                continue
            value = argument.value
            if isinstance(value, VariableNode):
                return int(variables[value.name.value])
            return int(value.value)
        return 1

    def walk(node, reachable: int) -> None:
        nonlocal peak
        for field in _selections(node, fragments):
            nodes = reachable * page_size(field)
            if nodes > peak[0]:
                peak = (nodes, field.name.value)
            walk(field, nodes)

    for operation in _operations(document):
        walk(operation, 1)
    return peak


def _node_limit_error(nodes: int, connection: str) -> dict:
    """GitHub's verbatim refusal of an over-budget query — the reply gh renders as
    `GraphQL: …` on stderr before exiting 1."""
    return {
        "errors": [
            {
                "message": (
                    f"By the time this query traverses to the {connection} "
                    f"connection, it is requesting up to {nodes:,} possible nodes "
                    f"which exceeds the maximum limit of {_MAX_NODES:,}."
                )
            }
        ]
    }


def _reject_unmodelled(
    fields: list[str], modelled: frozenset[str]
) -> tuple[int, object] | None:
    """The GraphQL error naming the first requested field the server does not
    model, or None when every one is answerable."""
    unmodelled = [f for f in fields if f not in modelled]
    if not unmodelled:
        return None
    return 200, {
        "errors": [
            {
                "message": (
                    f"fake GitHub: the query asks for '{unmodelled[0]}', which "
                    "this server does not model — gh would fill it with a zero "
                    "value and say nothing. Model it, or drop it from --json."
                )
            }
        ]
    }


def _pr_list_reply(nodes: list[dict]) -> tuple[int, object]:
    """GitHub's envelope around a one-page `pr list` result. gh parses this, so its
    shape is a contract every server answering a listing must meet identically."""
    return 200, {
        "data": {
            "repository": {
                "pullRequests": {
                    "totalCount": len(nodes),
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
    }


# What GitHub answers a listing that asks for a mergeability field over the whole
# open set: 502, because the listing then costs a mergeability computation per open
# PR. Run 31514730713 died that way on 2026-08-11 over 65 open PRs and three fields.
# The field names come from the production constant: a copy here would stop refusing
# the listing the moment a fourth field joins it there, and the regression test
# guarding that outage would pass over a fake that no longer punishes it.
_MERGEABILITY_FIELDS = _pr_sweep.MERGEABILITY_FIELDS

# GraphQL's three mergeability states, as the per-PR REST body spells them: a
# nullable boolean and a lower-case state. A double serving GraphQL's own names
# here would prove `_pr_sweep.mergeability_of` against nothing.
_REST_MERGEABILITY = {
    "MERGEABLE": (True, "clean"),
    "CONFLICTING": (False, "dirty"),
    "UNKNOWN": (None, "unknown"),
}


def _listing_refusal(fields: list[str]) -> tuple[int, object] | None:
    """GitHub's 502 for an open-PR listing that asks for mergeability, or None
    when the field set leaves that computation out."""
    if not _MERGEABILITY_FIELDS.intersection(fields):
        return None
    return 502, {"message": "Something went wrong while executing your query."}


def _rest_pull_reply(
    number: int,
    *,
    mergeable: str = "UNKNOWN",
    merge_state: str = "",
    armed: bool = False,
    head_sha: str = "",
    maintainer_can_modify: bool = False,
    omit_maintainer_can_modify: bool = False,
) -> tuple[int, object]:
    """One PR as `GET /repos/{o}/{r}/pulls/{n}` answers it. MERGE_STATE is
    GraphQL's spelling of `mergeStateStatus`, or "" to derive it from
    MERGEABLE. HEAD_SHA is this PR's head as REST reports it — the read that
    does not lag the push, so a caller can tell it from the listing's.

    MAINTAINER_CAN_MODIFY is whether this repository may push to the head branch.
    OMIT_MAINTAINER_CAN_MODIFY drops the key entirely, which is the answer a
    caller cannot read — a different state from a `false` it can."""
    # REST's `mergeable` is a nullable boolean, so it cannot name a GraphQL enum
    # member this table does not carry. `null` is its own "no verdict computed"
    # answer, and `mergeable_state` carries the member's spelling — the shape a
    # caller reading REST for a value only GraphQL can express really gets.
    flag, derived = _REST_MERGEABILITY.get(mergeable, (None, mergeable.lower()))
    return 200, {
        "number": number,
        "mergeable": flag,
        "mergeable_state": merge_state.lower() if merge_state else derived,
        "auto_merge": {"merge_method": "squash"} if armed else None,
        "head": {"sha": head_sha or f"sha-{number}"},
        **(
            {}
            if omit_maintainer_can_modify
            else {"maintainer_can_modify": maintainer_can_modify}
        ),
    }


def _queue_membership_reply(
    queued: bool | None, entry_state: str | None = None
) -> tuple[int, object]:
    """GitHub's envelope around lib/pr-merge-queue.bash's isInMergeQueue read.
    One builder for every server, so the fakes cannot drift from each other
    (nothing here ties the shape to the lib's --jq string). `None` is GitHub's
    answer for a pull request the query cannot resolve.

    `entry_state` is the queue's own judgement of the entry (GitHub's
    MergeQueueEntry.state; UNMERGEABLE is the one it will never build). `None`
    models a PR the queue holds no entry for."""
    entry = None if entry_state is None else {"state": entry_state}
    return 200, {
        "data": {
            "repository": {
                "pullRequest": {"isInMergeQueue": queued, "mergeQueueEntry": entry}
            }
        }
    }


def iso(age_seconds: int) -> str:
    """An ISO8601 UTC timestamp `age_seconds` in the past. Fixtures are computed
    against the real clock because the sweep measures age with `date -u +%s`."""
    return (datetime.now(UTC) - timedelta(seconds=age_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _self_signed(dir_: Path) -> Path:
    """A throwaway localhost cert+key pair, returning the combined cert path.

    Fails loud when `openssl` is absent rather than skipping the suite: a test
    that silently stops verifying the real CLI is worse than a red one naming
    the missing tool.
    """
    cert, key = dir_ / "cert.pem", dir_ / "key.pem"
    proc = run_capture(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        timeout=60,
    )
    assert cert.is_file() and key.is_file(), (
        f"could not mint a localhost cert (openssl rc={proc.returncode}); gh only "
        f"speaks TLS, so this suite cannot run without one:\n{proc.stderr}"
    )
    return cert


class _LocalGitHub:
    """A GitHub over TLS on localhost, plus the env that points real `gh` at it.

    Used as a context manager so the socket and serving thread die with the test.
    Subclasses answer requests by overriding `resolve`.
    """

    # The `--json` fields this server answers. gh fills any field its reply omits
    # with a zero value and says nothing, so an unmodelled field would reach the
    # script under test as ""/null — the silent drift a real server exists to
    # remove. `id` is not a script's to request: gh appends it to every `pr view`.
    modelled_fields: frozenset[str] = frozenset()

    def __init__(self, tmp_path: Path):
        # The one place every real-gh fixture passes through, so this refusal is
        # what keeps a darwin leg from reporting a TLS trust gap as a failure of
        # the script under test. See the module docstring for why gh cannot trust
        # this server's certificate there.
        if sys.platform == "darwin":
            pytest.skip(DARWIN_GH_TLS_SKIP)
        self.requests: list[tuple[str, str]] = []
        # The current request's query string, parsed. Set per request by the
        # handler because `resolve` is routed on the path alone.
        self.query: dict[str, list[str]] = {}
        # Extra response headers for the current request. The handler clears this
        # before each `resolve` and emits whatever that call left behind, which is
        # how a server that PAGES advertises its next page in `Link`.
        self.response_headers: dict[str, str] = {}

        (tmp_path / "home").mkdir(exist_ok=True)
        cert = _self_signed(tmp_path)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, tmp_path / "key.pem")
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        self.host = f"localhost:{self._server.server_address[1]}"
        # Annotated because the narrow pyright pass (pyrightconfig-tests.json)
        # skips unannotated-attribute inference: an un-annotated env reads as
        # Any there, and `env={**server.env, ...}` in a subprocess.run call then
        # fails overload matching at every consumer.
        self.env: dict[str, str] = {
            # An explicit PATH and a throwaway HOME: without them a child bash
            # falls back to a default PATH and gh reads the developer's real
            # ~/.config/gh, so the suite's behavior would depend on the host's
            # login state instead of only on this server.
            "PATH": current_path(),
            "HOME": str(tmp_path / "home"),
            "GH_HOST": self.host,
            "GH_TOKEN": "fixture-token",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "SSL_CERT_FILE": str(cert),
        }

    def paged(self, path: str, items: list) -> list:
        """One page of `items`, plus the `Link` header that names the next one.

        GitHub pages every list endpoint and advertises the next page in `Link`;
        `gh api --paginate` follows that header. A server that answered every item
        in one reply would let a caller that forgot `--paginate` pass this suite
        while it truncates against the real GitHub.
        """
        per_page = min(int(self.query.get("per_page", ["30"])[0]), 100)
        page = int(self.query.get("page", ["1"])[0])
        start = (page - 1) * per_page
        if start + per_page < len(items):
            # The next page carries THIS request's whole query, not just the
            # paging pair: a link that dropped the caller's filter would answer
            # page 2 from a different row set, so the pages a caller stitched
            # together would be missing exactly the rows the filter selected.
            params = {name: values[0] for name, values in self.query.items()}
            params |= {"per_page": str(per_page), "page": str(page + 1)}
            nxt = f"https://{self.host}{path}?{urlencode(params)}"
            self.response_headers["Link"] = f'<{nxt}>; rel="next"'
        return items[start : start + per_page]

    def dispatch(self, method: str, path: str, body: dict) -> tuple[int, object]:
        """Answer one request: the GHE probe every gh command opens with, then this
        server's own table, then the 404 that names what nobody modelled."""
        if path == "/api/v3/meta":
            # gh reads the Enterprise version out of this reply and PARSES it
            # before it will build a search query, so an empty object fails every
            # `gh pr list --search` on this server with a bare "malformed
            # version:" and no mention of the endpoint that produced it.
            return 200, {"installed_version": GHE_VERSION}
        answer = self.resolve(method, path, body)
        if answer is None:
            return 404, {"message": f"fake GitHub: unmodelled {method} {path}"}
        return answer

    def resolve(self, method: str, path: str, body: dict) -> tuple[int, object] | None:
        """The status and JSON (or raw bytes) body for one request this server
        models, or None to let `dispatch` answer the unmodelled 404."""
        raise NotImplementedError

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        assert not self._thread.is_alive(), "the fake GitHub thread did not stop"

    def paths(self, method: str) -> list[str]:
        return [path for verb, path in self.requests if verb == method]


@functools.lru_cache(maxsize=1)
def workflow_files_by_name() -> dict[str, tuple[str, ...]]:
    """Each real workflow's display name mapped to the files declaring it, from
    the same classifier the scripts under test read it with."""
    return merge_check_snapshot().files_by_workflow_name(
        REPO_ROOT / ".github" / "workflows"
    )


# The run conclusions that make a completed push run on main "red" for the
# two-tier gate this mixin serves — GitHub's full terminal-red vocabulary, so a
# scenario can seed any of them and still be found by the failed-run listing.
_FAILED_RUN_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "startup_failure", "action_required"}
)


class _MainRedRuns:
    """Mixin: the run/job history main_red_status.py's two-tier live read
    touches, plus the REST endpoints that serve it. Any fake GitHub whose
    script under test asks a main-red question mixes this in, calls
    `_main_red_init()` before its own state, and routes `resolve` through
    `_main_red_resolve` ahead of its own table.

    Tier 1 is one repo-wide listing of the newest failed push runs on main;
    tier 2 is one gating workflow's own run history, the way `gh run list
    --workflow` filters it. Empty by default, so a scenario that never calls
    `mark_main_red` exercises the ungated path.

    A workflow is reachable by NUMERIC ID and by FILE NAME, because `gh run
    list --workflow` resolves either and a caller passing a file must reach the
    same runs. The file comes from the real `.github/workflows/` tree, keyed by
    the display name the scenario seeds, so this server never invents a mapping
    the script classifies differently.
    """

    def _main_red_init(self) -> None:
        self.workflows: dict[str, int] = {}
        self.workflow_ids_by_file: dict[str, int] = {}
        self.shadow_workflows: list[dict] = []
        self.runs: list[dict] = []
        self.jobs: dict[int, list[dict]] = {}
        self._workflow_listing_breaks = False
        self._workflow_listing_breaks_after_first = False
        self._burst_listing_breaks = False
        self._workflow_listing_malformed = False
        self._malformed_jobs_ids: set[int] = set()
        self._red_after_first_listing: list[str] = []
        self._next_run_id = 1000

    def add_workflow(self, name: str) -> int:
        wf_id = self.workflows.setdefault(name, 10 + len(self.workflows))
        for file in workflow_files_by_name().get(name, ()):
            self.workflow_ids_by_file[file] = wf_id
        return wf_id

    def add_default_setup_workflow(self, name: str) -> None:
        """A workflow GitHub itself owns, sharing NAME with one of this tree's.

        Default setup (CodeQL) mints a workflow with no file in the repository,
        so a display name can address two workflows and `gh run list --workflow
        <name>` refuses the ambiguous reference. Only the file resolves.
        """
        self.shadow_workflows.append(
            {"id": 900 + len(self.shadow_workflows), "name": name, "state": "active"}
        )

    def _workflow_id(self, ref: str) -> int | None:
        """The id REF names, whether REF is a numeric id or a workflow file."""
        if ref.isdigit():
            return int(ref) if int(ref) in self.workflows.values() else None
        return self.workflow_ids_by_file.get(ref)

    def _name_of(self, wf_id: int) -> str:
        return next(name for name, got in self.workflows.items() if got == wf_id)

    def _file_of(self, wf_id: int) -> str:
        """WF_ID's file, or a derived one for a display name the real tree does
        not declare — a scenario naming an invented workflow still needs a
        `path`, which GitHub always serves."""
        for file, got in self.workflow_ids_by_file.items():
            if got == wf_id:
                return file
        return f"{self._name_of(wf_id).lower().replace(' ', '-')}.yaml"

    def add_run(
        self,
        workflow: str,
        conclusion: str | None,
        jobs: list[dict],
        *,
        event: str = "push",
        branch: str = "main",
        completed_at: str = OLD_COMPLETION,
        actor_type: str = "Bot",
    ) -> int:
        """One run of WORKFLOW, appended as the newest. `conclusion=None` is a
        run still in flight: status in_progress, no conclusion yet.

        COMPLETED_AT is served as `updated_at`, which is what a burst check
        measures its window against. It defaults far enough in the past that a
        scenario seeding a red without a time in mind never lands inside any
        window by accident.

        ACTOR_TYPE is the triggering actor's type. `User` on a
        `workflow_dispatch` is the one combination the notifier's own job-level
        `if:` denies, so it defaults to the unattended `Bot`."""
        self._next_run_id += 1
        run_id = self._next_run_id
        self.runs.append(
            {
                "id": run_id,
                "name": workflow,
                "workflow_id": self.add_workflow(workflow),
                "status": "completed" if conclusion else "in_progress",
                "conclusion": conclusion,
                "event": event,
                "head_branch": branch,
                "updated_at": completed_at,
                "triggering_actor": {"type": actor_type},
            }
        )
        self.jobs[run_id] = list(jobs)
        return run_id

    def mark_main_red(self, name: str, *, job: str = "job") -> int:
        """Seed NAME's newest push run on main as a failure no later run has
        repaired — the live equivalent of opening the old tracking issue
        blaming it. NAME still needs a real gating workflow file for the gate
        to act on it; a name no workflow declares is simply never a
        candidate, which is main_red_status.py's own fail-safe direction."""
        return self.add_run(name, "failure", [{"conclusion": "failure", "name": job}])

    def preamble_death(self, job: str = "job") -> dict:
        """One job that died in the runner's own preamble.

        GitHub serves `Set up job` as that job's single step, which is the shape
        a codeload 429 or an unresolvable action leaves behind: no step this
        repository wrote ever ran. Recorded from run 32028309787's cell-1 job on
        2026-08-17 — re-capture with
        `gh api repos/{owner}/{repo}/actions/runs/<id>/jobs --jq '.jobs[].steps'`.
        """
        return {
            "conclusion": "failure",
            "name": job,
            "steps": [
                {
                    "number": 1,
                    "name": "Set up job",
                    "status": "completed",
                    "conclusion": "failure",
                }
            ],
        }

    def ran_and_failed(self, job: str = "job") -> dict:
        """One job that reached a step this repository wrote and failed there —
        the contrast `preamble_death` is told apart from."""
        return {
            "conclusion": "failure",
            "name": job,
            "steps": [
                {
                    "number": 1,
                    "name": "Set up job",
                    "status": "completed",
                    "conclusion": "success",
                },
                {
                    "number": 2,
                    "name": "Run the suite",
                    "status": "completed",
                    "conclusion": "failure",
                },
            ],
        }

    def mark_main_red_by_infrastructure(self, name: str, *, job: str = "job") -> int:
        """Seed NAME's newest push run on main as a failure whose only job died
        in the runner's own preamble."""
        return self.add_run(name, "failure", [self.preamble_death(job)])

    def mark_main_red_after_first_listing(self, name: str) -> None:
        """NAME goes red only after the sweep's first main-red read — main
        going red while the sweep is already running, the window the
        pre-mutation re-read exists to close."""
        self._red_after_first_listing.append(name)

    def break_workflow_listing(self) -> None:
        """Fail the repo-wide failed-run listing, so the main-red gate cannot
        tell a red main from a clean one."""
        self._workflow_listing_breaks = True

    def break_burst_listing(self) -> None:
        """Fail the repo-wide failed-run listing the burst check reads, so the
        route script cannot tell one shared cause from an isolated red."""
        self._burst_listing_breaks = True

    def break_workflow_listing_after_first(self) -> None:
        """Serve the first failed-run listing and fail every later one: the
        gate's pre-mutation re-read going unreadable mid-sweep."""
        self._workflow_listing_breaks_after_first = True

    def serve_malformed_workflow_listing(self) -> None:
        """Answer the repo-wide failed-run listing 200 with no `workflow_runs`
        key — `gh api` prints this verbatim with no error, unlike
        `break_workflow_listing`'s HTTP failure, so this is the one way to
        reach `_failed_push_runs`'s own shape check rather than `gh`'s retry."""
        self._workflow_listing_malformed = True

    def serve_malformed_jobs(self, run_id: int) -> None:
        """Answer RUN_ID's jobs page 200 with no `jobs` key — the `gh api`
        counterpart to `serve_malformed_workflow_listing`, for `_run_jobs`'s
        own shape check."""
        self._malformed_jobs_ids.add(run_id)

    def _main_red_resolve(
        self, method: str, path: str, *, repo: str = "owner/repo"
    ) -> tuple[int, object] | None:
        """Every endpoint main_red_status.py's two-tier read touches, or None
        for anything else."""
        if method != "GET":
            return None
        base = f"/api/v3/repos/{repo}/actions"
        if path == f"{base}/runs" and self.query.get("event") == ["push"]:
            return self._tier1_reply(path)
        if path == f"{base}/runs" and self.query.get("status") == ["failure"]:
            return self._burst_reply(path)
        if path == f"{base}/workflows":
            rows = [
                {
                    "id": wf_id,
                    "name": name,
                    "state": "active",
                    "path": f".github/workflows/{self._file_of(wf_id)}",
                }
                for name, wf_id in self.workflows.items()
            ] + self.shadow_workflows
            return 200, {"total_count": len(rows), "workflows": self.paged(path, rows)}
        if match := re.fullmatch(rf"{re.escape(base)}/workflows/([^/]+)/runs", path):
            wf_id = self._workflow_id(match.group(1))
            if wf_id is None:
                return 404, {"message": f"fake GitHub: no workflow {match.group(1)}"}
            return self._tier2_reply(wf_id, path)
        if match := re.fullmatch(rf"{re.escape(base)}/workflows/([^/]+)", path):
            wf_id = self._workflow_id(match.group(1))
            if wf_id is None:
                return 404, {"message": f"fake GitHub: no workflow {match.group(1)}"}
            return 200, {
                "id": wf_id,
                "name": self._name_of(wf_id),
                "state": "active",
                "path": f".github/workflows/{self._file_of(wf_id)}",
            }
        if match := re.fullmatch(rf"{re.escape(base)}/runs/(\d+)/jobs", path):
            run_id = int(match.group(1))
            if run_id in self._malformed_jobs_ids:
                return 200, {"total_count": 0}
            jobs = self.jobs.get(run_id)
            if jobs is None:
                return 404, {"message": f"fake GitHub: no run {run_id}"}
            return 200, {"total_count": len(jobs), "jobs": self.paged(path, jobs)}
        return None

    def _tier1_reply(self, path: str) -> tuple[int, object]:
        """Tier 1: the newest LOOKBACK failed push runs on main, repo-wide."""
        if self._workflow_listing_breaks:
            return 500, {
                "message": "fake GitHub: the failed-run listing is unavailable"
            }
        if self._workflow_listing_malformed:
            return 200, {"total_count": 0}
        rows = [
            run
            for run in reversed(self.runs)
            if run["head_branch"] == "main"
            and run["event"] == "push"
            and run["conclusion"] in _FAILED_RUN_CONCLUSIONS
        ]
        served = (
            200,
            {
                "total_count": len(rows),
                "workflow_runs": self.paged(path, rows),
            },
        )
        # Changing state HERE, after the reply is built, is what makes the race
        # real rather than hand-wired: this listing answers green and every
        # later one answers red (or fails).
        for name in self._red_after_first_listing:
            self.mark_main_red(name)
        self._red_after_first_listing.clear()
        self._workflow_listing_breaks |= self._workflow_listing_breaks_after_first
        return served

    def _burst_reply(self, path: str) -> tuple[int, object]:
        """The repo-wide failed-run listing ci-failure-route.sh's burst check
        reads — every failed run, any event, branch, workflow and actor, newest
        first. The script itself drops the runs this notifier would not have
        paged for; serving them all here is what lets a test prove that it
        does."""
        if self._burst_listing_breaks:
            return 500, {
                "message": "fake GitHub: the failed-run listing is unavailable"
            }
        rows = [
            run
            for run in reversed(self.runs)
            if run["conclusion"] in _FAILED_RUN_CONCLUSIONS
        ]
        return 200, {
            "total_count": len(rows),
            "workflow_runs": self.paged(path, rows),
        }

    def _tier2_reply(self, wf_id: int, path: str) -> tuple[int, object]:
        """Tier 2: one gating workflow's own run history, filtered the way
        `gh run list --workflow` filters it — branch, event and status."""
        query = {key: values[0] for key, values in self.query.items()}
        wanted_status = query.get("status")
        rows = [
            run
            for run in reversed(self.runs)
            if run["workflow_id"] == wf_id
            and query.get("branch", run["head_branch"]) == run["head_branch"]
            and query.get("event", run["event"]) == run["event"]
            and (
                wanted_status is None
                or wanted_status in (run["status"], run["conclusion"])
            )
        ]
        return 200, {
            "total_count": len(rows),
            "workflow_runs": self.paged(path, rows),
        }


class _MergeQueueGitHub(_MainRedRuns, _LocalGitHub):
    """several fake servers each model GitHub's merge queue.

    The resolver, the re-arm sweep and the ready-PR cap all probe the same queue,
    so their servers hold the same state and answer the same read. One definition
    here: separate copies drift from GitHub in separate directions, and a test
    double that has drifted proves the script correct against a queue that does
    not exist.

    The state is set in construction rather than by a method each server calls,
    so a server cannot inherit the model and then not have it. Each subclass sets
    its own state first and ends with `super().__init__(tmp_path)`, which lands
    the queue state before `_LocalGitHub` starts the serving thread.
    """

    def __init__(self, tmp_path: Path):
        # Which pull requests the queue holds.
        self.in_merge_queue: set[int] = set()
        # The entries GitHub has judged UNMERGEABLE: held, never built, never
        # evicted. A caller that waits for the queue to eject one waits forever.
        self.unmergeable_queue_entries: set[int] = set()
        self._main_red_init()
        super().__init__(tmp_path)

    def _merge_queue_reply(self, number: int) -> tuple[int, object]:
        """The answer to one isInMergeQueue probe for `number`.

        Outages are NOT modelled here. Each server spells one its own way — the
        re-arm server breaks a read per number and orders that against its
        mid-sweep window — and one shared flag beside those would be a second
        spelling with a precedence nothing states."""
        return _queue_membership_reply(
            number in self.in_merge_queue,
            "UNMERGEABLE"
            if number in self.unmergeable_queue_entries
            else "QUEUED"
            if number in self.in_merge_queue
            else None,
        )


class _IssueCommentStore:
    """The four issue-comment endpoints, for every fake a sticky poster drives.

    A sticky comment is defined by what it does to the comment LIST — it rewrites
    its own marked comment and never stacks a second one — so the list is the
    state a test must read, and it is what the real `gh api` calls mutate.

      GET   /api/v3/repos/{o}/{r}/issues/{n}/comments   (find the marked comment)
      GET   /api/v3/repos/{o}/{r}/issues/comments/{id}  (read its current body)
      PATCH /api/v3/repos/{o}/{r}/issues/comments/{id}  (rewrite it in place)
      POST  /api/v3/repos/{o}/{r}/issues/{n}/comments   (first run only)

    A mixin rather than a second copy: a poster that also reads check runs needs
    the same four endpoints beside them, and two stores would answer the same
    PATCH differently the day one of them gained a case.
    """

    def _init_comments(self) -> None:
        self.comments: list[dict] = []
        # Every PATCH this server served, in order. GitHub sends an
        # `issue_comment: edited` webhook for each one — including a PATCH that
        # writes the body already there — so a test that wants to prove a run woke
        # nobody has to count the calls, not read the resulting bodies.
        self.patched: list[int] = []
        self.fail_listings = False
        self._next_id = 1001

    def add_comment(self, body: str) -> int:
        """Seed an existing comment, standing in for a previous run's post."""
        self._next_id += 1
        self.comments.append({"id": self._next_id, "body": body})
        return self._next_id

    def bodies(self) -> list[str]:
        return [comment["body"] for comment in self.comments]

    def resolve_comment(
        self, method: str, path: str, body: dict
    ) -> tuple[int, object] | None:
        """The answer for a comment path, or None when PATH is not one."""
        listing = f"/api/v3/repos/{self.repo}/issues/{self.pr}/comments"
        if path == listing:
            if method == "GET":
                # A failed listing answers 500, never an empty page: the lookup
                # must keep "could not read" apart from "no match", and a server
                # answering [] here would let one collapsing the two pass green.
                if self.fail_listings:
                    return 500, {"message": "fake GitHub: the listing is unavailable"}
                return 200, self.paged(path, self.comments)
            if method == "POST":
                return 201, {"id": self.add_comment(body["body"])}
            return None

        match = re.fullmatch(
            rf"/api/v3/repos/{re.escape(self.repo)}/issues/comments/(?P<id>\d+)", path
        )
        if match is None:
            return None
        wanted = int(match.group("id"))
        for comment in self.comments:
            if comment["id"] != wanted:
                continue
            if method == "GET":
                return 200, comment
            if method == "PATCH":
                comment["body"] = body["body"]
                self.patched.append(wanted)
                return 200, comment
        return None


class FakeIssueComments(_IssueCommentStore, _LocalGitHub):
    """A GitHub holding one PR's issue comments and nothing else."""

    def __init__(self, tmp_path: Path, *, repo: str = "owner/repo", pr: int = 7):
        self.repo = repo
        self.pr = pr
        self._init_comments()
        super().__init__(tmp_path)
        # A deterministic server never recovers on retry, so backoff would only
        # slow the failure tests.
        self.env |= {"RETRY_MAX": "1"}

    def resolve(self, method: str, path: str, body: dict) -> tuple[int, object] | None:
        return self.resolve_comment(method, path, body)


class FakeActionsArtifacts(_LocalGitHub):
    """The repo's artifacts and this workflow file, so tests drive the real
    `gh api` reads reuse-bundle.py makes: the name-filtered artifact listing,
    the workflow-file lookup its producer pin compares against, and the zip
    download — which real GitHub answers with a 302 to blob storage that gh must
    follow, so this server redirects too.

    `artifacts` is newest first, the order the real listing answers in; `zips`
    maps artifact id to the bytes the blob redirect serves.
    """

    # The workflow file whose artifacts this server owns, and the numeric id
    # GitHub mints for it. Any other file name 404s, so a probe reading the
    # wrong workflow fails loudly rather than pinning against a plausible id.
    WORKFLOW_FILE = "auto-resolve-conflicts.yaml"
    WORKFLOW_ID = 4242

    def __init__(
        self, tmp_path: Path, *, repo: str = "owner/repo", branch: str = "main"
    ):
        self.repo = repo
        # The branch the reconciler dispatches this workflow on, which is what
        # an artifact's own run must have been produced from.
        self.branch = branch
        self.artifacts: list[dict] = []
        self.zips: dict[int, bytes] = {}
        self.fail_listings = False
        super().__init__(tmp_path)

    def add_artifact(
        self,
        artifact_id: int,
        name: str,
        *,
        expired: bool = False,
        run_id: int = 100,
        head_branch: str | None = None,
        workflow_id: int | None = None,
    ) -> dict:
        """Register one artifact as the NEWEST, the position the listing answers
        first. `workflow_run` carries only the three fields the producer pin
        reads, the smallest redaction of the real listing's run block."""
        row = {
            "id": artifact_id,
            "name": name,
            "expired": expired,
            "workflow_run": {
                "id": run_id,
                "head_branch": self.branch if head_branch is None else head_branch,
                "workflow_id": self.WORKFLOW_ID if workflow_id is None else workflow_id,
            },
        }
        self.artifacts.insert(0, row)
        return row

    def resolve(self, method: str, path: str, body: dict) -> tuple[int, object] | None:
        actions = f"/api/v3/repos/{self.repo}/actions"
        if path == f"{actions}/artifacts":
            # A failed listing answers 500, never an empty page: the probe must
            # keep "could not read" apart from "no prior artifact", and a server
            # answering [] here would let a script collapsing the two pass green.
            if self.fail_listings:
                return 500, {
                    "message": "fake GitHub: the artifact listing is unavailable"
                }
            wanted = self.query.get("name", [""])[0]
            rows = [row for row in self.artifacts if row["name"] == wanted]
            return 200, {"total_count": len(rows), "artifacts": self.paged(path, rows)}
        if match := re.fullmatch(
            rf"{re.escape(actions)}/workflows/(?P<file>[^/]+)", path
        ):
            if match.group("file") != self.WORKFLOW_FILE:
                return 404, {"message": "fake GitHub: no such workflow file"}
            return 200, {
                "id": self.WORKFLOW_ID,
                "path": f".github/workflows/{self.WORKFLOW_FILE}",
            }
        if match := re.fullmatch(
            rf"{re.escape(actions)}/artifacts/(?P<artifact>\d+)/zip", path
        ):
            artifact_id = int(match.group("artifact"))
            if artifact_id in self.zips:
                self.response_headers["Location"] = (
                    f"https://{self.host}/blob/{artifact_id}"
                )
                return 302, b""
        if match := re.fullmatch(r"/blob/(?P<artifact>\d+)", path):
            artifact_id = int(match.group("artifact"))
            if artifact_id in self.zips:
                return 200, self.zips[artifact_id]
        return None


@dataclass(frozen=True)
class ResolverPR:  # pylint: disable=too-many-instance-attributes
    """One PR as a discovery test states it. Everything GitHub-shaped — the label
    objects, the author's bot decoration, the commit connection — is built from
    these fields by the server, so no test writes an API shape.

    `mergeable` may be a tuple, which GitHub settles through one entry per query
    that asks for it: that is how mergeability really arrives, since a read of a
    PR GitHub has not computed yet answers UNKNOWN.

    `commit_ages` are hours before now, because the window under test is
    evaluated against `now` — a frozen timestamp would drift out of it and rot.
    """

    number: int
    head_ref: str = "feature"
    base_ref: str = "main"
    # Empty means "derive one from the number" (see `sha`). A shared literal
    # default would give two PRs the same head COMMIT, which then has to carry
    # both their commit dates — the head-commit read has no way to tell them
    # apart, and neither would GitHub.
    head_sha: str = ""
    state: str = "OPEN"
    draft: bool = False
    cross_repo: bool = False
    mergeable: str | tuple[str, ...] = "CONFLICTING"
    labels: tuple[str, ...] = ()
    author: str = "a-human"
    bot: bool = False
    commit_ages: tuple[float, ...] = (1,)
    # Hours before now of each time this PR came back from draft to
    # ready-for-review. Empty is a PR opened ready and left ready. A TUPLE because
    # the cap throttle drafts and readies the same PR repeatedly, so several of
    # these is the normal shape here — and a single value could not tell a reader
    # that takes the NEWEST from one that takes the first it finds.
    ready_for_review_ages: tuple[float, ...] = ()
    # Who GitHub attributes the HEAD COMMIT to. Empty means the PR's own author
    # still owns the branch, which is the ordinary case; state it only for the
    # branch somebody else has pushed to.
    head_commit_author: str = ""
    # How many commits ahead of its base carry two parents. Non-zero says the
    # head already holds a merge from the base, which is what tells a manual
    # chain from a native stack — the latter requires linear history.
    merge_commits: int = 0
    # A comparison whose first page does not cover the range. GitHub serves
    # `compare` oldest-first and pages only under `--paginate`, so a branch more
    # than a page ahead of its base hides its newest commits — the position a
    # merge from the base sits in.
    compare_truncated: bool = False
    # The head SHA the GraphQL LISTING serves, when it lags the real one. Empty
    # is the ordinary case, where both reads agree. GitHub's listing trails a
    # push by minutes, so a scan that keys on it acts on a head nobody pushed.
    stale_listed_sha: str = ""
    # Whether this repository may push to the head branch, as REST's
    # `maintainer_can_modify` reports it. Only a FORK head is judged on it.
    maintainer_can_modify: bool = False
    # True serves a REST pull object with NO `maintainer_can_modify` key: the
    # answer a scan could not read, which is not the same as a `false` it could.
    maintainer_answer_absent: bool = False
    # True serves null for both head-repository fields, which is what GitHub
    # answers once the fork behind a pull request is deleted.
    head_repo_deleted: bool = False

    @property
    def head_repo(self) -> str:
        """`owner/name` of the repository the head branch lives on — this
        repository, or a fork of it for a cross-repository PR."""
        if self.head_repo_deleted:
            return ""
        return "forker/repo" if self.cross_repo else "owner/repo"

    @property
    def sha(self) -> str:
        """This PR's head commit sha — the stated one, or one derived from the
        number so PRs a test did not distinguish still differ."""
        return self.head_sha or f"sha-{self.number}"

    @property
    def listed_sha(self) -> str:
        """The head SHA the open-PR listing reports, which the per-PR REST read
        corrects when it lags."""
        return self.stale_listed_sha or self.sha

    @property
    def head_author_login(self) -> str:
        """The head commit author as REST spells it — a bot account carries the
        ``[bot]`` suffix there, where the GraphQL listing writes ``app/<login>``."""
        login = self.head_commit_author or self.author
        return f"{login}[bot]" if self.bot and not self.head_commit_author else login


class FakeResolverGitHub(_MergeQueueGitHub):
    """A running GitHub for one auto-resolve discovery test: PR state, commit
    statuses (the attempt mark) and merge-queue membership."""

    modelled_fields = frozenset(
        {
            "number",
            "mergeable",
            "isDraft",
            "isCrossRepository",
            "headRepository",
            "headRepositoryOwner",
            "headRefName",
            "headRefOid",
            "baseRefName",
            "state",
            "labels",
            "author",
            "commits",
            "id",
        }
    )

    def __init__(self, tmp_path: Path, prs: list[ResolverPR]):
        self.prs = {pr.number: pr for pr in prs}
        self.statuses: dict[str, list[dict]] = {}
        # The next id this server assigns a status write, real GitHub's own and
        # increasing per write — the ordering a claim arbitration reads to tell a
        # pre-seeded mark from one a script under test writes later.
        self._next_status_id = 1
        # Every (sha, context) this server accepted a status write for, in order.
        # A claim check that stands down must post NOTHING, and only the write log
        # tells that apart from a claim check that re-marks and then stands down.
        self.status_writes: list[tuple[str, str]] = []
        # True 502s the status WRITE, the shape mark-attempt must refuse to spend
        # through rather than proceed on an unmarked head.
        self.status_write_fails = False
        # True 502s the status LIST GET. Falls toward UNCLAIMED: a read blip that
        # stranded a conflicted PR would cost more than the one duplicate resolve
        # it saves.
        self.status_read_fails = False
        # What `GET /rate_limit` answers, and whether it answers at all. The
        # budget that refuses a scan's calls refuses this endpoint too.
        now = int(time.time())
        self.rate_limit_read_fails = False
        self.rate_limit_buckets = {
            "core": {"limit": 5000, "remaining": 4321, "reset": now + 600},
            "graphql": {"limit": 5000, "remaining": 4999, "reset": now + 600},
        }
        # "first" or "last": a COMPETING run's attempt mark is stored around the
        # next attempt write this server accepts, taking the id that ordering
        # gives it. This is the race the pre-read cannot see — both runs read an
        # unmarked head, so only the ids GitHub assigned can arbitrate. Cleared
        # once it fires, so a re-mark does not race itself forever.
        self.racing_mark_lands: str | None = None
        self.comments: dict[int, list[str]] = {}
        # Hours since each base branch last moved, served as the branch tip's
        # committer date; a branch not listed moved long ago, so every mark a
        # test writes postdates the move and models "the base has not moved".
        self.branch_moved_hours_ago: dict[str, float] = {}
        # True 502s the branch-tip read, which must fall toward retrying.
        self.branch_tip_read_fails = False
        # Hours since the resolver's own code last changed, per path under
        # RESOLVER_PATHS. A path not listed changed long ago, so a mark a test
        # writes is NEWER than that change: the direction that HOLDS the mark.
        self.resolver_changed_hours_ago: dict[str, float] = {}
        # True 502s the resolver-history read, which must HOLD a handoff mark: an
        # outage is no evidence the resolver changed.
        self.resolver_history_read_fails = False
        # How many resolver-history reads this server served, and which paths they
        # asked about. Only the count against the DISTINCT paths tells a cache that
        # works from one that re-asks once per handed-off PR: both give every PR
        # the same verdict.
        self.resolver_history_reads = 0
        self.resolver_history_paths: list[str] = []
        # Paths this server answers 200 with an EMPTY history for: the shape a
        # renamed or deleted RESOLVER_PATHS entry takes, which is not a read
        # failure and must not read as one.
        self.resolver_history_absent: set[str] = set()
        # How many branch-tip reads this server actually served. The caller caches
        # the answer per base ref, and only a count can tell a cache that works
        # from one that re-asks: both give every PR the same verdict.
        self.branch_tip_reads = 0
        # True fails the whole server's queue probe, which is how a test drives
        # the discovery's no-data path.
        self.merge_queue_probe_fails = False
        # An outage on the ready-for-review probe. The window must fall back to
        # the head commit alone, never widen, so this direction needs a test.
        self.ready_probe_fails = False
        # An outage on the base...head comparison. A chain the scan cannot
        # characterise must stay refused, so this direction needs a test too.
        self.compare_probe_fails = False
        # The `gh pr view` a PR-event run scopes itself with. An outage here is
        # the one the whole scan rests on: with nothing else to read, a discovery
        # that survives it has decided "no conflicts" from no data at all.
        self.single_pr_read_fails = False
        # One entry per GraphQL operation served, which is what a test asserts on
        # to show a retry loop stopped: the REST-level `requests` cannot tell a
        # listing from a merge-queue probe, since both are POST /api/graphql.
        self.operations: list[str] = []
        self._mergeable_reads: dict[int, int] = {}
        self.tmp_path = tmp_path
        self._output = tmp_path / "github-output"
        super().__init__(tmp_path)
        self.env |= {"REPO": "owner/repo"}

    def mark_attempt(self, sha: str, hours_ago: float = 0) -> None:
        """Record that the resolver already ran against `sha`."""
        self._add_status(sha, "auto-resolve/attempted", hours_ago)

    def mark_handoff(self, sha: str, hours_ago: float = 0) -> None:
        """Record that a paid run left `sha`'s remaining conflicts to a human."""
        self._add_status(sha, "auto-resolve/handed-off", hours_ago)

    def mark_declined(self, sha: str, hours_ago: float = 0) -> None:
        """Record that the model read `sha`'s conflicts and refused them."""
        self._add_status(sha, "auto-resolve/declined", hours_ago)

    def release_attempt(self, sha: str, hours_ago: float = 0) -> None:
        """Record that the run holding `sha`'s mark handed it back."""
        self._add_status(sha, "auto-resolve/attempted-released", hours_ago)

    def _add_status(self, sha: str, context: str, hours_ago: float) -> None:
        status_id = self._next_status_id
        self._next_status_id += 1
        self.statuses.setdefault(sha, []).append(
            {
                "id": status_id,
                "context": context,
                "state": "success",
                "description": "auto-resolve",
                "created_at": iso(int(hours_ago * 3600)),
            }
        )

    def mark_attempt_script(self, repo_dir: Path, **env):
        """Run the real mark-attempt.sh against this server, in `repo_dir`.

        Returns (result, outputs) where outputs is the parsed $GITHUB_OUTPUT.
        """
        out = self.tmp_path / "mark-output"
        out.write_text("", encoding="utf-8")
        res = run_capture(
            ["bash", str(MARK_ATTEMPT_SCRIPT)],
            env={
                **self.env,
                "GITHUB_OUTPUT": str(out),
                "RETRY_DELAY_SECS": "0",
                "RETRY_BASE_DELAY": "0",
                **env,
            },
            cwd=repo_dir,
            timeout=180,
        )
        outputs = dict(
            line.split("=", 1)
            for line in out.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        return res, outputs

    def discover(self, *, pr_number: int | None = None, max_passes: int = 1, **env):
        """Run the real discover script against this server."""
        self._output.write_text("", encoding="utf-8")
        full_env = {
            **self.env,
            "GITHUB_OUTPUT": str(self._output),
            "MAX_PASSES": str(max_passes),
            "RETRY_DELAY_SECS": "0",
            # Keep all five ci-retry attempts, drop their backoff: the test that
            # drives a refused gh call would otherwise spend 30s per call
            # sleeping through a failure it is asserting on.
            "RETRY_BASE_DELAY": "0",
        }
        if pr_number is not None:
            full_env["PR_NUMBER"] = str(pr_number)
        return run_capture(
            [sys.executable, str(DISCOVER_SCRIPT)],
            env=full_env | coverage_env() | env,
            timeout=180,
        )

    @property
    def output_text(self) -> str:
        """Everything the last discovery wrote to $GITHUB_OUTPUT, verbatim — for
        a test asserting that it wrote NO verdict, which `emitted` cannot say
        because it requires exactly one."""
        return self._output.read_text(encoding="utf-8")

    @property
    def emitted(self) -> list[dict]:
        """The PR list the last discovery wrote to $GITHUB_OUTPUT."""
        rows = [
            line for line in self.output_text.splitlines() if line.startswith("prs=")
        ]
        assert len(rows) == 1, f"discover wrote {len(rows)} `prs=` lines, expected 1"
        return json.loads(rows[0].removeprefix("prs="))

    @property
    def listings(self) -> int:
        """How many open-PR listings ran — one per retry pass."""
        return self.operations.count("PullRequestList")

    def _node(self, pr: ResolverPR, projected: list[str]) -> dict:
        """One PR as GraphQL returns it, carrying every modelled field: gh exports
        only the ones its caller asked for, so a superset keeps the projection in
        one place."""
        return {
            "number": pr.number,
            "mergeable": self._mergeable(pr, "mergeable" in projected),
            "isDraft": pr.draft,
            "isCrossRepository": pr.cross_repo,
            # Two separate objects, as gh's own projection serves them, and both
            # null once the head repository is gone.
            "headRepository": (
                None if pr.head_repo_deleted else {"name": pr.head_repo.split("/")[1]}
            ),
            "headRepositoryOwner": (
                None if pr.head_repo_deleted else {"login": pr.head_repo.split("/")[0]}
            ),
            "headRefName": pr.head_ref,
            "headRefOid": pr.listed_sha,
            "baseRefName": pr.base_ref,
            "state": pr.state,
            "id": f"PR_{pr.number}",
            "labels": {
                "nodes": [
                    {
                        "id": f"LA_{name}",
                        "name": name,
                        "description": "",
                        "color": "ededed",
                    }
                    for name in pr.labels
                ],
                "totalCount": len(pr.labels),
            },
            # A GitHub App's author is a Bot, which carries no `id` — the `... on
            # User` fragment gh requests matches nothing. That absence is how gh
            # decides an author is a bot, and it is why a bot login reaches a
            # script spelled `app/<login>`: gh adds the prefix, GitHub does not.
            "author": (
                {"login": pr.author}
                if pr.bot
                else {"login": pr.author, "id": f"U_{pr.number}", "name": pr.author}
            ),
            # Truncated at 100 with no cursor, as `gh pr view --json commits`
            # asks for it (`commits(first: 100)`). GitHub keeps the OLDEST 100,
            # so a PR past that reads as stale through this field.
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "oid": f"{pr.sha}-{i}",
                            "committedDate": iso(int(age * 3600)),
                            "authoredDate": iso(int(age * 3600)),
                            "messageHeadline": "a commit",
                            "messageBody": "",
                            "authors": {
                                "nodes": [{"name": "A Human", "email": "a@b.c"}]
                            },
                        }
                    }
                    for i, age in enumerate(pr.commit_ages[:100])
                ]
            },
        }

    def _timeline(self, number: int) -> list[dict]:
        """One PR's issue timeline, in the REST shape observed on this repo:

            gh api repos/{owner}/{repo}/issues/3571/timeline?per_page=100

        answered `{"event": "ready_for_review", "created_at": "2026-08-05T07:11:13Z"}`.

        Three properties of the real endpoint this models, each of which a
        one-entry fixture would let a broken reader past:

        * it is NOT filtered by event type, so a reader that forgets to select
          `ready_for_review` takes a `labeled` date instead — the decoys are
          dated NOW, so taking one puts an aged-out PR back inside the window;
        * it is PAGINATED, so on a busy PR the entry the caller wants sits past
          page one. More than a page of decoys means a reader that drops
          `--paginate` truncates before reaching it and judges the PR on its
          commit date alone — this change's own bug, resurrected for exactly the
          PRs with the most history;
        * it ASCENDS by `created_at`, so the newest `ready_for_review` is LAST.
          A reader that takes the first one it finds gets the oldest.

        The decoys straddle the events on purpose. The old ones fill page one, so
        the `ready_for_review` entries land on page two and only a `--paginate`
        read reaches them. The one dated NOW sorts last, so a read that skips the
        event filter takes it and calls an aged-out PR fresh. Either decoy alone
        leaves one of those two mistakes green."""
        ready = [
            {"event": "ready_for_review", "created_at": iso(int(age * 3600))}
            for age in self.prs[number].ready_for_review_ages
        ]
        older_than_any_event = iso(_TIMELINE_DECOY_AGE_SECS)
        entries = [
            *({"event": "labeled", "created_at": older_than_any_event},) * 101,
            *ready,
            {"event": "labeled", "created_at": iso(0)},
        ]
        return sorted(entries, key=lambda entry: entry["created_at"])

    def mergeability_reads(self, number: int) -> int:
        """How many reads of this PR's mergeability the scan spent. Counted only
        for a PR whose `mergeable` is a SEQUENCE, since a fixed value serves every
        read without consuming one."""
        return self._mergeable_reads.get(number, 0)

    def _mergeable(self, pr: ResolverPR, consumes: bool) -> str:
        """This read's answer for a PR whose mergeability GitHub is still
        settling. Only a read that ASKS for mergeability consumes a step, so a
        `--json commits` fetch leaves the sequence where it was."""
        if isinstance(pr.mergeable, str):
            return pr.mergeable
        read = self._mergeable_reads.get(pr.number, 0)
        if consumes:
            self._mergeable_reads[pr.number] = read + 1
        return pr.mergeable[min(read, len(pr.mergeable) - 1)]

    def _graphql(self, body: dict) -> tuple[int, object]:
        """Answer one query, parsed as GraphQL rather than scanned as text: gh's
        rendering is not this server's to predict, and a mis-read projection
        would silently shrink the checks below."""
        variables = body.get("variables", {})
        document = parse(body.get("query", ""))
        fragments = _fragments(document)
        operation = _operations(document)[0].name
        nodes, connection = _node_estimate(document, fragments, variables)
        if nodes > _MAX_NODES:
            self.operations.append("refused")
            return 200, _node_limit_error(nodes, connection)

        if _projects(document, fragments, "isInMergeQueue"):
            self.operations.append("isInMergeQueue")
            if self.merge_queue_probe_fails:
                return 502, {"message": "merge-queue probe outage"}
            return self._merge_queue_reply(variables["number"])

        if operation and operation.value == "PullRequestList":
            fields = _projected_fields(document, fragments)
            self.operations.append("PullRequestList")
            refusal = _listing_refusal(fields)
            if refusal:
                return refusal
            rejected = _reject_unmodelled(fields, self.modelled_fields)
            if rejected:
                return rejected
            listed = [
                self._node(pr, fields)
                for pr in self.prs.values()
                if pr.state in variables["state"]
            ]
            return _pr_list_reply(listed)

        if operation and operation.value == "PullRequestByNumber":
            self.operations.append("PullRequestByNumber")
            if self.single_pr_read_fails:
                return 502, {"message": "fake GitHub: single-PR read outage"}
            fields = _projected_fields(document, fragments)
            rejected = _reject_unmodelled(fields, self.modelled_fields)
            if rejected:
                return rejected
            pr = self.prs[variables["pr_number"]]
            return 200, {
                "data": {"repository": {"pullRequest": self._node(pr, fields)}}
            }

        self.operations.append("unmodelled")
        return 200, {
            "errors": [{"message": f"fake GitHub: unmodelled operation {operation}"}]
        }

    def resolve(self, method: str, path: str, body: dict) -> tuple[int, object] | None:
        if path == "/api/graphql":
            return self._graphql(body)
        if method == "GET" and path == "/api/v3/rate_limit":
            # Served with the real document's shape, because the scan reports the
            # budget it has left and a 404 here would make that line read
            # "budget unread" in every test — the value the reporting exists to
            # show would then never be exercised.
            if self.rate_limit_read_fails:
                return 403, {"message": RATE_LIMIT_REFUSAL}
            return 200, {"resources": self.rate_limit_buckets}
        match = _STATUSES_RE.match(path)
        if match and method == "GET":
            if self.status_read_fails:
                return 502, {"message": "status read outage"}
            return 200, self.statuses.get(match.group("sha"), [])
        match = _STATUS_WRITE_RE.match(path)
        if match and method == "POST":
            if self.status_write_fails:
                return 502, {"message": "status write outage"}
            sha = match.group("sha")
            context = body.get("context", "")
            self.status_writes.append((sha, context))
            racing = (
                self.racing_mark_lands if context == "auto-resolve/attempted" else None
            )
            if racing:
                self.racing_mark_lands = None
            if racing == "first":
                self._add_status(sha, context, 0)
            status_id = self._next_status_id
            self._next_status_id += 1
            self.statuses.setdefault(sha, []).append(
                {**body, "id": status_id, "created_at": iso(0)}
            )
            if racing == "last":
                self._add_status(sha, context, 0)
            return 201, {"context": body.get("context", ""), "id": status_id}
        match = _BRANCH_RE.match(path)
        if match and method == "GET":
            if self.branch_tip_read_fails:
                return 502, {"message": "branch read outage"}
            self.branch_tip_reads += 1
            branch = match.group("branch")
            hours = self.branch_moved_hours_ago.get(branch, 10_000)
            return 200, {
                "name": branch,
                "commit": {
                    "sha": f"tip-{branch}",
                    "commit": {"committer": {"date": iso(int(hours * 3600))}},
                },
            }
        if path.endswith("/commits") and method == "GET" and "path" in self.query:
            if self.resolver_history_read_fails:
                return 502, {"message": "commit history read outage"}
            self.resolver_history_reads += 1
            asked = self.query["path"][0]
            self.resolver_history_paths.append(asked)
            if asked in self.resolver_history_absent:
                return 200, []
            hours = self.resolver_changed_hours_ago.get(asked, 10_000)
            return 200, [{"commit": {"committer": {"date": iso(int(hours * 3600))}}}]
        match = _PULL_RE.match(path)
        if match and method == "GET":
            self.operations.append("mergeability")
            pr = self.prs[int(match.group("pr"))]
            return _rest_pull_reply(
                pr.number,
                mergeable=self._mergeable(pr, True),
                head_sha=pr.sha,
                maintainer_can_modify=pr.maintainer_can_modify,
                omit_maintainer_can_modify=pr.maintainer_answer_absent,
            )
        match = _PR_COMMITS_RE.match(path)
        if match and method == "GET":
            ages = self.prs[int(match.group("pr"))].commit_ages
            # GitHub serves at most 250 commits from this endpoint, oldest first,
            # however many pages the caller asks for. Modelling that ceiling is
            # what makes a read of a longer branch observably short.
            commits = [
                {"commit": {"committer": {"date": iso(int(age * 3600))}}}
                for age in ages[:250]
            ]
            return 200, self.paged(path, commits)
        match = _COMPARE_RE.match(path)
        if match and method == "GET":
            self.operations.append("compare")
            if self.compare_probe_fails:
                return 502, {"message": "fake GitHub: compare outage"}
            head_ref = match.group("head")
            pr = next(
                (p for p in self.prs.values() if p.head_ref == head_ref),
                None,
            )
            if pr is None:
                return 404, {"message": "fake GitHub: no such head"}
            # Only the parent COUNT is read, so one parent entry per ordinary
            # commit and two per merge is the whole shape this endpoint owes.
            # `total_commits` is the range's real size, which the page under-
            # reports when the branch runs past one page.
            commits = [
                *[{"parents": [{"sha": "p1"}, {"sha": "p2"}]}] * pr.merge_commits,
                {"parents": [{"sha": "p1"}]},
            ]
            if pr.compare_truncated:
                return 200, {"commits": [commits[-1]], "total_commits": len(commits)}
            return 200, {"commits": commits, "total_commits": len(commits)}
        match = _COMMIT_RE.match(path)
        if match and method == "GET":
            # One commit by sha. The head commit is a branch's newest, so it
            # carries the smallest of that PR's commit ages.
            pr = next(p for p in self.prs.values() if p.sha == match.group("sha"))
            newest = iso(int(min(pr.commit_ages) * 3600))
            return 200, {
                "commit": {"committer": {"date": newest}},
                "author": {"login": pr.head_author_login},
            }
        match = _TIMELINE_RE.match(path)
        if match and method == "GET":
            self.operations.append("timeline")
            if self.ready_probe_fails:
                return 502, {"message": "fake GitHub: timeline outage"}
            return 200, self.paged(path, self._timeline(int(match.group("pr"))))
        match = _ISSUE_COMMENTS_RE.match(path)
        if match:
            pr = int(match.group("pr"))
            posted = self.comments.setdefault(pr, [])
            if method == "GET":
                return 200, [{"body": text} for text in posted]
            posted.append(body["body"])
            return 201, {"id": len(posted)}
        return None


@functools.cache
def merge_check_snapshot():
    """merge-check-snapshot.py, the parser the sweeps read the workflow tree
    through — so a test and the code under it cannot disagree about what a
    workflow's `on:` means."""
    path = REPO_ROOT / ".github" / "scripts" / "merge-check-snapshot.py"
    spec = importlib.util.spec_from_file_location("merge_check_snapshot", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeHeadRuns(_LocalGitHub):
    """A running GitHub for the run-full-tests re-run sweep:

      GET  /api/v3/repos/{o}/{r}/pulls/{n}                (the pull request's LIVE head)
      GET  /api/v3/repos/{o}/{r}/actions/runs?head_sha=…  (the head's runs, paged)
      POST /api/v3/repos/{o}/{r}/actions/runs/{id}/rerun  (restart one run)

    `live_head` is what the pull request's head is RIGHT NOW, which a test moves
    away from the sweep's `HEAD_SHA` to model a push that landed between the
    label and this job.

    The listing filters by `head_sha` the way GitHub does and pages at
    `per_page`, so a sweep that drops the filter or reads one page fails
    behaviorally. `refuse_rerun` holds the run ids GitHub answers 403 for — a run
    too old to restart — which is the only way to reach the arm that must red
    rather than report a clean sweep.

    The live head MOVES the way a real one does: `move_head_after` names how many
    re-runs GitHub serves before a push lands, so a sweep that reads the head once
    before its loop still restarts runs on a head the pull request left behind.
    `pull_status` and `refuse_pull` drive the two refused reads the sweep must
    fail open on.
    """

    def __init__(self, tmp_path: Path, *, repo: str = "owner/repo"):
        self.repo = repo
        self.runs: list[dict] = []
        self.rerun: list[int] = []
        self.refuse_rerun: set[int] = set()
        self.live_head = "a" * 40
        self.moved_head = "b" * 40
        self.move_head_after: int | None = None
        self.pull_status = 200
        self.refuse_pull = False
        self._next_id = 500
        self._workflow_ids: dict[str, int] = {}
        super().__init__(tmp_path)
        self.env |= {"GITHUB_REPOSITORY": repo, "RETRY_MAX": "1"}

    def add_run(
        self,
        name: str,
        *,
        run_id: int | None = None,
        head_sha: str = "headsha",
        status: str = "completed",
    ) -> int:
        """One run of the workflow called NAME, as the listing reports it.

        Runs sharing a NAME share a `workflow_id`, the way every run of one
        workflow file does — which is what lets a test put two runs of the sweep
        itself on one head.
        """
        if run_id is None:
            self._next_id += 1
            run_id = self._next_id
        self.runs.append(
            {
                "id": run_id,
                "name": name,
                "workflow_id": self._workflow_ids.setdefault(
                    name, 900 + len(self._workflow_ids)
                ),
                "status": status,
                "head_sha": head_sha,
            }
        )
        return run_id

    def resolve(self, method: str, path: str, body: dict) -> tuple[int, object] | None:
        del body
        prefix = f"/api/v3/repos/{self.repo}/actions/runs"
        if method == "GET" and path == prefix:
            wanted = (self.query.get("head_sha") or [""])[0]
            rows = [run for run in self.runs if run["head_sha"] == wanted]
            return 200, {
                "total_count": len(rows),
                "workflow_runs": self.paged(path, rows),
            }
        if method == "GET" and re.fullmatch(
            rf"/api/v3/repos/{re.escape(self.repo)}/pulls/\d+", path
        ):
            if self.refuse_pull:
                return 403, {"message": "fake GitHub: this token may not read the PR"}
            if self.pull_status != 200:
                return self.pull_status, {"message": "fake GitHub: refused"}
            return 200, {"head": {"sha": self.live_head}}
        match = re.fullmatch(rf"{re.escape(prefix)}/(?P<id>\d+)/rerun", path)
        if method == "POST" and match:
            run_id = int(match["id"])
            if run_id in self.refuse_rerun:
                return 403, {"message": "fake GitHub: this run is too old to re-run"}
            self.rerun.append(run_id)
            if self.move_head_after is not None and len(self.rerun) >= (
                self.move_head_after
            ):
                self.live_head = self.moved_head
            return 201, {}
        return None


class FakeRateLimit(_LocalGitHub):
    """`GET /rate_limit`, the one endpoint `_gh_rate_limit.py` reads.

    A real server rather than a `gh` stub because the belief under test IS the
    response shape: which buckets the document carries, that `remaining` and
    `reset` sit inside `resources.<bucket>`, and that `reset` is epoch seconds.
    A stub of `gh` would assert that reading back to itself.
    """

    def __init__(
        self,
        tmp_path: Path,
        *,
        core_remaining: int,
        core_reset_in: float,
        graphql_remaining: int = 5000,
        graphql_reset_in: float = 3600,
        raw_body: bytes | None = None,
        refusal: str | None = None,
        refuse_rate_limit_read: bool = False,
    ):
        # When set, every path other than `/rate_limit` answers 403 with this
        # message — the shape of the installation-scoped and secondary limits,
        # which refuse the call itself while the buckets still show requests
        # remaining.
        self.refusal = refusal
        self.refuse_rate_limit_read = refuse_rate_limit_read
        # When set, `/rate_limit` answers 200 with these bytes instead of the
        # document. A middlebox that returns an HTML page under a 200 is the real
        # shape of this: `gh` succeeds and hands the caller something no JSON
        # reader can take.
        self.raw_body = raw_body
        now = int(time.time())
        self.buckets = {
            "core": {
                "limit": 5000,
                "remaining": core_remaining,
                "reset": now + int(core_reset_in),
                "used": 5000 - core_remaining,
            },
            "graphql": {
                "limit": 5000,
                "remaining": graphql_remaining,
                "reset": now + int(graphql_reset_in),
                "used": 5000 - graphql_remaining,
            },
        }
        super().__init__(tmp_path)

    def resolve(self, method: str, path: str, body: dict) -> tuple[int, object] | None:
        if method == "GET" and path == "/api/v3/rate_limit":
            if self.refuse_rate_limit_read:
                # The budget that refuses a call refuses this endpoint too, so
                # the reader meant to explain the refusal cannot be served either.
                return 403, {"message": RATE_LIMIT_REFUSAL}
            if self.raw_body is not None:
                return 200, self.raw_body
            # `rate` is GitHub's deprecated top-level echo of `core`; it is served
            # so a reader that took it by mistake still meets the real shape.
            return 200, {"resources": self.buckets, "rate": self.buckets["core"]}
        if self.refusal is not None:
            return 403, {"message": self.refusal}
        return None


def _handler_for(state: _LocalGitHub):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a) -> None:  # no stderr access log in test output
            pass

        def _send(self, status: int, payload: object) -> None:
            json_body = not isinstance(payload, bytes)
            body = json.dumps(payload).encode() if json_body else payload
            self.send_response(status)
            content_type = (
                "application/json" if json_body else "application/octet-stream"
            )
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in state.response_headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length)) if length else {}

        def _route(self) -> None:
            state.requests.append((self.command, self.path))
            path, _, query = self.path.partition("?")
            # `resolve` is routed on the path alone, so a server that PAGES reads
            # its cursor from here rather than re-parsing the path everywhere.
            state.query = parse_qs(query)
            state.response_headers = {}
            self._send(*state.dispatch(self.command, path, self._body()))

        do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _route

    return Handler
