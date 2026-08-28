"""How many resolution shards this runner can hold at once.

PROBLEM CLASS — a concurrency chosen as a constant is wrong on every runner but
the one it was measured on. Too low leaves the fan-out's window unspent on a
wide conflict set, and every round then re-buys the whole set. Too high is not a
slow run but a dead one: the OOM killer picks a process, and when it picks the
fan-out driver the job publishes nothing and every finished shard dies with the
runner. So the caller's number is an intent, and this says what the machine can
hold.
"""

from pathlib import Path

#: What one shard reserves. A shard is a `claude` process holding the file it
#: rewrites, its own transcript, and the hooks its edits fire. Deliberately an
#: over-estimate: the ceiling only ever LOWERS what the caller asked for, so
#: guessing high costs some concurrency and guessing low costs the job.
SHARD_MEMORY_MB = 512
#: Left for everything that is not a shard — the driver, git, the checkout.
RESERVED_MEMORY_MB = 1024
#: Where the free-memory meter is read. A name rather than a literal, so a test
#: can point it at a runner it does not have.
MEMINFO = Path("/proc/meminfo")


def available_memory_mb() -> int | None:
    """This runner's MemAvailable in MiB, or None where nothing reports it.

    None is "no meter", never "no memory": an unreadable meter must leave the
    caller's number alone rather than silently serialize the fan-out.
    """
    try:
        meminfo = MEMINFO.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in meminfo.splitlines():
        if line.startswith("MemAvailable:"):
            fields = line.split()
            if len(fields) >= 2 and fields[1].isdigit():
                return int(fields[1]) // 1024
    return None


def memory_ceiling(asked: int) -> int:
    """ASKED, lowered to the number of shards this runner's free memory holds.

    At least one shard always runs — a fan-out that launches nothing resolves
    nothing, and a runner too small for one shard fails in the shard, where the
    failure names itself.
    """
    available = available_memory_mb()
    if available is None:
        return asked
    holds = max(1, (available - RESERVED_MEMORY_MB) // SHARD_MEMORY_MB)
    if holds >= asked:
        return asked
    print(
        f"::notice::running {holds} shard(s) at once, not {asked}: "
        f"{available} MiB is free and one shard reserves {SHARD_MEMORY_MB} MiB."
    )
    return holds
