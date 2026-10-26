"""Extract tool_name and absolute target path from a PreToolUse payload.

Used by safe-launch.sh when the wrapped hook fails to parse, to decide
whether the in-flight tool call is a self-repair edit on a hook file.

Reads the PreToolUse JSON from stdin and prints two lines: tool_name,
<<<<<<< local
then the absolute file path (or an empty line if none). safe-launch reads
those by position, so a value holding a newline would shift the field
below it: a payload naming ".claude/hooks/x\\nBash" could hand safe-launch
a tool name its own payload never had. Such a value is refused here. On
any parse failure, prints nothing so safe-launch falls through to the
fail-safe "ask" default.
||||||| base
then the absolute file path (or an empty line if none). On any parse
failure, or if the parsed JSON is not an object, exits 0 with empty
output so safe-launch falls through to the fail-safe "ask" default.
=======
then the absolute file path (or an empty line if none). safe-launch reads
those BY LINE, so a value holding a newline would split into fields of its
own: a Write to ".claude/hooks/x\\n/../../../target" would hand safe-launch
the hook-dir half as the path. Such a value, or one that is not a string,
prints nothing. On any parse failure, or if the parsed JSON is not an
object, it also prints nothing, so safe-launch falls through to the
fail-safe "ask" default.
>>>>>>> template
"""

import json
import os
import sys


<<<<<<< local
def main() -> None:
||||||| base
def main() -> int:
=======
def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def main() -> int:
>>>>>>> template
    if len(sys.argv) != 2:
        return
    project_dir = sys.argv[1]
    try:
        data = json.loads(sys.stdin.read())
<<<<<<< local
    except ValueError:
        return
||||||| base
    except (json.JSONDecodeError, ValueError):
        return 0
=======
    except ValueError:
        return 0
>>>>>>> template
    if not isinstance(data, dict):
<<<<<<< local
        return
    name = data.get("tool_name")
    name = name if isinstance(name, str) else ""
    tool_input = data.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    raw_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    path = raw_path if isinstance(raw_path, str) else ""
||||||| base
        return 0
    name = data.get("tool_name", "") or ""
    tool_input = data.get("tool_input", {}) or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
=======
        return 0
    name = _string(data.get("tool_name"))
    tool_input = data.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    path = _string(tool_input.get("file_path") or tool_input.get("notebook_path"))
>>>>>>> template
    # MultiEdit carries an array of edits; use the first entry's file_path.
    if not path and name == "MultiEdit":
<<<<<<< local
        edits = tool_input.get("edits")
        if isinstance(edits, list) and edits and isinstance(edits[0], dict):
            first = edits[0].get("file_path")
            path = first if isinstance(first, str) else ""
||||||| base
        edits = tool_input.get("edits") or []
        if edits and isinstance(edits, list) and isinstance(edits[0], dict):
            path = edits[0].get("file_path") or ""
=======
        edits = tool_input.get("edits")
        if isinstance(edits, list) and edits and isinstance(edits[0], dict):
            path = _string(edits[0].get("file_path"))
>>>>>>> template
    if path and not os.path.isabs(path):
        path = os.path.join(project_dir, path)
<<<<<<< local
    # A newline or carriage return embedded in either value can't name a real
    # self-repair target, so fail safe to empty output (the "ask" default).
    if any(c in name or c in path for c in ("\n", "\r")):
        return
||||||| base
=======
    # This refusal is what stops a newline-split path from passing safe-launch's
    # containment check on its first half.
    if any(c in name or c in path for c in ("\n", "\r")):
        return 0
>>>>>>> template
    print(name)
    print(path)


if __name__ == "__main__":
    main()
