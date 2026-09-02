"""Shared helpers used by multiple test modules.

Lives in a regular module (not `conftest.py`) so it can be imported directly
without manipulating `sys.path` or relying on the conftest plugin loader.
"""

import os
import re
import types
from importlib import util as importlib_util
from importlib.machinery import SourceFileLoader
import shutil
import subprocess
from pathlib import Path


def _repo_root() -> Path:
    """Repo root from git itself, anchored at this file's directory (not the
    caller's cwd), so moving test files can never silently repoint it the way
    depth-based parent-walking does."""
    toplevel = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return Path(toplevel)


REPO_ROOT = _repo_root()

GIT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def git_env() -> dict[str, str]:
    """Environment for running git in test sandboxes."""
    return {**os.environ, **GIT_IDENTITY_ENV}


def init_test_repo(path: Path) -> None:
    """Init a throwaway repo with signing/hooks disabled so fixtures can commit
    in any environment (including CI runners with enforced commit signing)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    for k, v in [
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
        ("user.name", "t"),
        ("user.email", "t@t"),
        ("core.hooksPath", "/dev/null"),
    ]:
        subprocess.run(["git", "config", "--local", k, v], cwd=path, check=True)


def commit_all(repo: Path, message: str = "fixture") -> str:
    """Stage everything and create a commit; returns the resulting SHA."""
    env = git_env()
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", message],
        cwd=repo,
        env=env,
        check=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return sha.stdout.strip()


def commit_files(repo: Path, files: dict[str, str], message: str) -> str:
    """Write `files` (repo-relative path -> content) into `repo`, stage everything
    and commit; returns the new SHA. Parent dirs are created, so a case can add a
    file in a directory the fixture repo does not have yet."""
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return commit_all(repo, message)


def git_out(repo: Path, *args: str) -> str:
    """Run git in `repo` with the test identity env and return its stripped
    stdout, raising on a non-zero exit."""
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=git_env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def run_capture(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    """`subprocess.run` with the capture_output/text/check defaults every test
    uses. `kwargs` (env, cwd, input, ...) are forwarded verbatim."""
    return subprocess.run(args, capture_output=True, text=True, check=False, **kwargs)


_SCRIPT_DIRS = [
    REPO_ROOT / ".github" / "scripts",
    REPO_ROOT / ".claude" / "hooks",
    REPO_ROOT / ".hooks",
]


def copy_script_to(script_name: str, dest_dir: Path) -> Path:
    """Copy a repo script into `dest_dir`, preserving the executable bit."""
    for src_dir in _SCRIPT_DIRS:
        src = src_dir / script_name
        if src.exists():
            dest = dest_dir / script_name
            shutil.copy2(src, dest)
            dest.chmod(0o755)
            return dest
    raise FileNotFoundError(f"Could not find {script_name} in any known location")


def current_path() -> str:
    """The live PATH, so a hermetic test env can still resolve git/bash."""
    return os.environ.get("PATH", "/usr/bin:/bin")


def write_exe(path: Path, body: str) -> Path:
    """Write `body` to `path`, mark it executable, and return it.

    Written to a temp sibling and renamed on: opening the path directly
    truncates it, which fails with ETXTBSY while a prior exec of the same stub
    is still draining. A rename over the busy inode is never blocked.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.chmod(0o755)
    os.replace(tmp, path)
    return path


def coverage_env() -> dict[str, str]:
    """The one variable that turns measurement on inside a child interpreter.

    A test builds a child's environment from scratch, so without this a Python
    script driven as a subprocess reports 0% however thoroughly it is tested.
    Empty when the parent run is not measuring, so an ordinary run writes no
    data files.
    """
    start = os.environ.get("COVERAGE_PROCESS_START")
    return {"COVERAGE_PROCESS_START": start} if start else {}


def load_script(rel: str) -> types.ModuleType:
    """Import the script at REPO_ROOT/<rel> in-process, under a module name
    derived from its filename: suffix dropped, every non-identifier character
    mapped to `_`. A hyphenated `.github/` script is not a legal import name,
    so the loader is named explicitly rather than derived from the path."""
    path = REPO_ROOT / rel
    loader = SourceFileLoader(re.sub(r"\W", "_", path.stem), str(path))
    spec = importlib_util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise ImportError(f"no module spec for {path}")
    module = importlib_util.module_from_spec(spec)
    loader.exec_module(module)
    return module
