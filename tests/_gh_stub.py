"""Shell body for a `gh api -i` refusal: status line and JSON body on stdout, message on stderr, exit 1."""

import json
import shlex


def refusal_body(message: str, *, reset_epoch: int | None = None) -> str:
    """Bash for a `gh` stub that refuses like `gh api -i` does on an HTTP 403.

    With `reset_epoch`, the response carries the `x-ratelimit-*` headers a primary
    rate-limit refusal sends; without it, none (a secondary-limiter refusal).
    """
    headers = (
        f"x-ratelimit-remaining: 0\\r\\nx-ratelimit-reset: {reset_epoch}\\r\\n"
        if reset_epoch is not None
        else ""
    )
    return (
        f"printf 'HTTP/2.0 403 Forbidden\\r\\n{headers}\\r\\n'; "
        f"echo {shlex.quote(json.dumps({'message': message}))}; "
        f"echo {shlex.quote(message)} >&2; exit 1"
    )
