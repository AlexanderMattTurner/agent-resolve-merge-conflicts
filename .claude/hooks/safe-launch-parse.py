"""Extract tool_name and absolute target path from a PreToolUse payload.

Used by safe-launch.sh when the wrapped hook fails to parse, to decide
whether the in-flight tool call is a self-repair edit on a hook file.

Reads the PreToolUse JSON from stdin and prints one JSON object with
"tool_name" and "tool_path" keys (path empty if none). A line-oriented
format cannot carry these: a value holding a newline shifts the field
below it, so a payload naming ".claude/hooks/x\\nBash" could hand
safe-launch a tool name its own payload never had. On any parse failure,
prints nothing so safe-launch falls through to the fail-safe "ask"
default.
"""

import json
import os
import sys


def main() -> None:
    if len(sys.argv) != 2:
        return
    project_dir = sys.argv[1]
    try:
        data = json.loads(sys.stdin.read())
    except ValueError:
        return
    if not isinstance(data, dict):
        return
    name = data.get("tool_name")
    name = name if isinstance(name, str) else ""
    tool_input = data.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    raw_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    path = raw_path if isinstance(raw_path, str) else ""
    # MultiEdit carries an array of edits; use the first entry's file_path.
    if not path and name == "MultiEdit":
        edits = tool_input.get("edits")
        if isinstance(edits, list) and edits and isinstance(edits[0], dict):
            first = edits[0].get("file_path")
            path = first if isinstance(first, str) else ""
    if path and not os.path.isabs(path):
        path = os.path.join(project_dir, path)
    # A newline or carriage return embedded in either value can't name a real
    # self-repair target, so fail safe to empty output (the "ask" default).
    if any(c in name or c in path for c in ("\n", "\r")):
        return
    json.dump({"tool_name": name, "tool_path": path}, sys.stdout)


if __name__ == "__main__":
    main()
