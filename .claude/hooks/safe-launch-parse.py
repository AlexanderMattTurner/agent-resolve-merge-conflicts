"""Extract tool_name and absolute target path from a PreToolUse payload.

Used by safe-launch.sh when the wrapped hook fails to parse, to decide
whether the in-flight tool call is a self-repair edit on a hook file.

Reads the PreToolUse JSON from stdin and prints two lines: tool_name,
then the absolute file path (or an empty line if none). safe-launch reads
those by position, so a value holding a newline could shift or split the
fields below it: a payload naming the tool ".claude/hooks/x\nBash" could
hand safe-launch a tool name its own payload never had, and a Write to
".claude/hooks/x\n/../../../target" could hand safe-launch the hook-dir
half as the path, passing its containment check while the real target
escapes it. Such a value, or one that is not a string, prints nothing. On
any parse failure, or if the parsed JSON is not an object, it also prints
nothing, so safe-launch falls through to the fail-safe "ask" default.
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
        return 0
    if not isinstance(data, dict):
        return
    name = _string(data.get("tool_name"))
    tool_input = data.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    path = _string(tool_input.get("file_path") or tool_input.get("notebook_path"))
    # MultiEdit carries an array of edits; use the first entry's file_path.
    if not path and name == "MultiEdit":
        edits = tool_input.get("edits")
        if isinstance(edits, list) and edits and isinstance(edits[0], dict):
            path = _string(edits[0].get("file_path"))
    if path and not os.path.isabs(path):
        path = os.path.join(project_dir, path)
    # A newline or carriage return embedded in either value can't name a real
    # self-repair target, so fail safe to empty output (the "ask" default).
    if any(c in name or c in path for c in ("\n", "\r")):
        return
    print(name)
    print(path)


if __name__ == "__main__":
    main()
