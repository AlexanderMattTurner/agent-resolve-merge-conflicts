"""How long a markdown code fence must be to hold a given text.

PROBLEM CLASS — a report quotes bytes a pull request controls, and a fixed
three-backtick fence lets those bytes close it early and spill the rest as
prose the reviewer reads as the report's own. One backtick longer than the
longest run inside the text is what makes that unreachable, so every fenced
block this repository writes takes its delimiter from here.
"""

import re

_RUNS = re.compile(r"`+")


def fence(text: str) -> str:
    """The fence delimiter for TEXT: one backtick longer than its longest run,
    and never shorter than markdown's own three."""
    longest = max((len(run.group()) for run in _RUNS.finditer(text)), default=0)
    return "`" * max(3, longest + 1)
