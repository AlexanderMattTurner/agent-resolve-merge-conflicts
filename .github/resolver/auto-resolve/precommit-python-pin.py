#!/usr/bin/env python3
"""Print the `X.Y` Python version a caller's `.pre-commit-config.yaml` pins.

pre-commit builds every `language: python` hook's virtualenv with the
interpreter `default_language_version.python` names, found on PATH. A runner
image that dropped that version fails the hook install, and the resolver reads
the failure as a resolution its hooks refused. `install-hook-tools.sh` installs
the version this prints. It prints nothing when the caller has no config, pins
no python, or names the ambient interpreter (`python3`, `python`). Any other
value is not a version this script can install, so it exits 1 and names it.

Usage: precommit-python-pin.py <path to .pre-commit-config.yaml>
"""

import re
import sys
from pathlib import Path

import yaml

AMBIENT = {"python", "python3", "default", "system"}


def pin(config: Path) -> str:
    if not config.is_file():
        return ""
    doc = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    value = (doc.get("default_language_version") or {}).get("python")
    if value is None or value in AMBIENT:
        return ""
    match = re.fullmatch(r"python(3\.[0-9]+)", str(value))
    if match is None:
        sys.exit(
            f"{config}: default_language_version.python is {value!r}, which is "
            "not a `python3.N` executable name, so no version can be installed for it"
        )
    return match.group(1)


if __name__ == "__main__":
    print(pin(Path(sys.argv[1])))
