#!/usr/bin/env python3
"""The calling workflow's file name, read from `$GITHUB_WORKFLOW_REF`.

PROBLEM CLASS — a script that names the caller's workflow file by hand. The
reusable resolver runs inside whatever caller a repository wrote, and the
dispatches, the artifact producer pin and the usage records all have to name
THAT file. A hard-coded `auto-resolve-conflicts.yaml` dispatches nothing, and
pins against a 404, in a repository whose caller has any other name. GitHub sets
`GITHUB_WORKFLOW_REF` to the caller's ref in every job of a called workflow.

Run as a script, it prints the file name, so the shell steps read the same answer.
"""

import os
from pathlib import PurePosixPath


def workflow_file(workflow_ref: str) -> str:
    """`owner/repo/.github/workflows/x.yaml@refs/heads/main` → `x.yaml`.

    Raises ValueError for a ref with no workflow file in it, so a malformed value
    stops the caller instead of naming a file that does not exist."""
    path = PurePosixPath(workflow_ref.split("@", maxsplit=1)[0])
    if path.parent.name != "workflows" or path.suffix not in (".yaml", ".yml"):
        raise ValueError(f"not a workflow ref: {workflow_ref!r}")
    return path.name


def caller_workflow_file() -> str:
    """The file name of the workflow this run belongs to. Raises KeyError when
    `GITHUB_WORKFLOW_REF` is unset, which is never true on a runner."""
    return workflow_file(os.environ["GITHUB_WORKFLOW_REF"])


if __name__ == "__main__":
    print(caller_workflow_file())
