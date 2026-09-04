# shellcheck shell=bash
# kcov-exclude: untraceable run: lib.test.mjs drives protected_matches via Node's execFileSync("bash", ["-c", 'source "${LIB}"; protected_matches "$@"', …]) — outside the Python kcov interceptor's reach (it only wraps Python's subprocess) and, even ignoring that, argv[1] is source text, so no invocation names the tracked path. The PREPARE, BUNDLE and LAND workflow steps that source the rest of it run only on a real conflicted PR.
# Shared by the auto-resolve PREPARE, BUNDLE and LAND steps (sourced, not run).
#
# Three invariants bind every editor here:
#   * CONFLICT_MARKER_RE is the ONE spelling both these shell steps and
#     bundle.py grep with; a second copy drifts, and a Python step then finds
#     no markers on a merge the shell steps refuse.
#   * A marker verdict needs the COMPLETE triple. `=======` alone is legal
#     Markdown and ordinary banner art, so one kind would call prose damage.
#   * structural_solve accepts only exit 0 AND non-empty output AND no
#     `<<<<<<<`. mergiraf exits 0 printing nothing when it cannot solve, and
#     PREPARE copies this output over the file, so empty is silent data loss.
# shellcheck source=.github/resolver/lib/shared-names.bash
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/shared-names.bash"
# Where the Python helpers beside this file live, for the functions that shell out to one.
AUTO_RESOLVE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Marks an unresolved hunk; also matches `|||||||`, the diff3 base section.
CONFLICT_MARKER_RE="$(shared_name .auto_resolve.conflict_marker_re)"

# Ref carrying the resolved merge across the job boundary; not under refs/heads/.
# shellcheck disable=SC2034  # read by the scripts that source this file, never here
AUTO_RESOLVE_RESULT_REF="$(shared_name .auto_resolve.result_ref)"

# protected_matches PATH… — subset touching security-sensitive trees. Override with AUTO_RESOLVE_PROTECTED_RE (an ERE).
# The default names only the trees EVERY consumer has, because this resolver now
# runs against repositories whose layouts it does not know. A caller with more to
# protect passes the workflow's `protected-paths-regex` input; one that passes
# nothing still gets its automation and agent config flagged.
protected_matches() {
  local protected="${AUTO_RESOLVE_PROTECTED_RE:-^(\.github/|\.claude/|\.hooks/)}" f
  for f in "$@"; do
    [[ "$f" =~ $protected ]] && printf '%s\n' "$f"
  done
  return 0
}

# configure_merge_conflict_style — write diff3 conflict markers here, so a conflict
# keeps its merge-base section between `|||||||` and `=======`.
# The style is the one _conflict_hunks.py pins for `git merge-file`, which reads no
# config: two spellings would leave a relocation-ported file shaped unlike the rest.
CONFLICT_STYLE="$(shared_name .auto_resolve.conflict_style.worktree)"
configure_merge_conflict_style() {
  git config merge.conflictStyle "$CONFLICT_STYLE"
}

# override_unsafe_merge_attributes — bind the types structural_merge_unsafe names to git's built-in line merge, for THIS checkout only.
# The resolve job runs install-mergiraf.sh inside the CONSUMER's checkout, and that script binds `merge.mergiraf.driver`. So a consumer whose own `.gitattributes` says `*.yaml merge=mergiraf` gets the block-scalar drop during the resolver's own `git merge`, on a binding this action activated. The resolver cannot edit their tree, and should not: it writes `$GIT_DIR/info/attributes`, which `gitattributes(5)` ranks ABOVE the in-tree file, and which lives in an ephemeral checkout.
# Appends rather than truncates, because a consumer may already keep entries there.
override_unsafe_merge_attributes() {
  local git_dir attrs path
  # --git-common-dir, NOT --git-dir: in a linked worktree the latter is
  # .git/worktrees/<name>, while git reads info/attributes from the COMMON dir.
  # Writing to the wrong one fails open with exit 0. land.sh makes worktrees
  # from this same library, so the wrong call is a live trap.
  git_dir="$(git rev-parse --git-common-dir)" || return 1
  mkdir -p "$git_dir/info" # bare-mkdir-ok: the write below is the post-condition and fails loudly
  attrs="$git_dir/info/attributes"

  # PER PATH, and only where the path would reach mergiraf TODAY. A blanket `*.yaml merge=text` here is silent lockfile corruption: this file outranks the whole attribute stack, so it beats the consumer's own `pnpm-lock.yaml -merge`, re-enables the line merge that rule refuses, and flips the unmergeable verdict to false so the file leaves the unresolvable partition. Narrowing cannot: a `-merge` path resolves to `unset`, never to `mergiraf` or `unspecified`.
  # -z, and EVERY tracked path filtered through structural_merge_unsafe rather than a pathspec. Two fail-opens otherwise, each leaving the file bound to mergiraf for the whole merge: `git ls-files` C-quotes a non-ASCII or control-character name under the default core.quotepath — the printed form gains surrounding double quotes and octal escapes — and check-attr then matches that literal against nothing; and a `*.yaml` pathspec is case-sensitive, so `Config.YAML` is never listed while `mergiraf solve` would still key on it.
  local candidates bound
  candidates="$(mktemp)"
  bound="$(mktemp)"
  while IFS= read -r -d "" path; do
    structural_merge_unsafe "$path" && printf '%s\0' "$path"
  done < <(git ls-files -z) >"$candidates"
  # Fails CLOSED: this whole function exists to stop a silent content drop, and a
  # reader that crashed would leave every candidate bound to the driver that
  # drops it. Written to a file so the exit status is readable.
  if ! python3 "$AUTO_RESOLVE_DIR/_merge_attr.py" --bound-to-structural-driver \
    --root . <"$candidates" >"$bound"; then
    rm -f "$candidates" "$bound"
    echo "auto-resolve: could not read which paths the structural merge driver is bound to; refusing rather than merging them under a driver that drops content." >&2
    return 1
  fi
  rm -f "$candidates"
  {
    echo "# Written by auto-resolve/prepare: these paths lose content under the structural merge driver."
    while IFS= read -r -d "" path; do
      # Quote every path: a gitattributes pattern takes C-style quoting, and an unquoted name with a space would parse as a pattern plus an attribute.
      printf '"%s" merge=text\n' "${path//\"/\\\"}"
    done <"$bound"
  } >>"$attrs"
  rm -f "$bound"
}

# structural_merge_unsafe PATH — true when the syntax-aware merge DROPS content on this file type, so it must never run.
# mergiraf v0.18.0 resolves two sides that each append inside one YAML block scalar by keeping ours and DROPPING theirs: it reports `Solved 1 conflict` and exits 0, so the drop reaches the branch with no marker and nothing in the diff to show it. `run: |` is the commonest shape in a workflow, which is where a consumer's conflicts land. On TOML it writes a DUPLICATE table and reports the merge solved — agent-glovebox PR #4569 emitted `[project]` twice, once per side, which no TOML parser accepts.
# This is SEPARATE from `.gitattributes`, which binds only the git merge DRIVER. `mergiraf solve` rebuilds from conflict markers and reads no attribute, so a consumer whose tree says `merge=text` still reaches the drop through PREPARE without this.
# Refusing routes the file to the model, which is the correct home for a conflict no deterministic pass can settle. Override with AUTO_RESOLVE_STRUCTURAL_SKIP_RE (an ERE); an EMPTY value keeps the default, unlike harness_unwritable_matches, because this bound exists to stop silent data loss and no consumer should be able to disable it by passing nothing.

# The ONE definition of the set, in shared-names.json, because _merge_attr.py reads it too: a path this set names is one the resolver UNBINDS from the structural driver, so a classification computed without the same list answers `mergiraf` in a job that did not write `$GIT_DIR/info/attributes`. `override_unsafe_merge_attributes` needs patterns and `structural_merge_unsafe` needs an ERE, so the ERE is DERIVED below rather than written a second time — two hand-kept copies would need a drift test, the shape code-style.md bans.
# shellcheck disable=SC2034  # read by PREPARE and by the loop above
_STRUCTURAL_SKIP_GLOB_LINES="$(shared_name_list .auto_resolve.structural_skip_globs)"
mapfile -t STRUCTURAL_SKIP_GLOBS <<<"$_STRUCTURAL_SKIP_GLOB_LINES"
# Refuses an EMPTY set rather than deriving `\.()$`, which matches no path and so
# would let mergiraf run on every YAML and TOML file — the silent content drop
# this whole set exists to stop, reached by a read that returned nothing.
_structural_skip_re_from_globs() {
  local glob out=""
  for glob in "${STRUCTURAL_SKIP_GLOBS[@]}"; do
    out+="${out:+|}${glob#\*.}"
  done
  [[ -n "$out" ]] || {
    printf 'auto-resolve: shared-names.json listed no structural_skip_globs\n' >&2
    return 1
  }
  printf '\\.(%s)$' "$out"
}
STRUCTURAL_SKIP_DEFAULT_RE="$(_structural_skip_re_from_globs)"
structural_merge_unsafe() {
  local skip="${AUTO_RESOLVE_STRUCTURAL_SKIP_RE:-$STRUCTURAL_SKIP_DEFAULT_RE}"
  # Case-INSENSITIVE: `mergiraf solve` keys on the real filename and reads no
  # attribute, so `Config.YAML` reaches the drop while git's own globs (which
  # are case-sensitive) would miss it. Scoped with a local shopt so the caller's
  # matching is unchanged.
  local restore
  restore="$(shopt -p nocasematch)"
  shopt -s nocasematch
  [[ "$1" =~ $skip ]]
  local rc=$?
  eval "$restore"
  return $rc
}

# structural_solve BIN FILE OUT — write the syntax-aware merge to OUT; 0 only if solved COMPLETELY. Three acceptance conditions, each load-bearing:
# * exit 0 — the tool ran and claims success;
# * NON-EMPTY output — mergiraf exits 0 and prints nothing when it cannot solve, and PREPARE copies this output over the conflicted file, so accepting empty is silent data loss reported as a solve;
# * NO MARKER of any kind but `=======` — a partial solve still carries markers, and the file must reach the LLM byte-identical to what git wrote when anything is left. `<<<<<<<` alone is not the test: mergiraf rewrites a hunk it partly understands and can leave `|||||||` and `>>>>>>>` with no opening marker, which prepare then stages, so the file leaves the unmerged set, never reaches the model, and bundle refuses the WHOLE run after the fan-out is paid for. `=======` stays allowed because a solved file may hold one as ordinary text. `-p` prints and leaves FILE alone, which is what makes the byte-identical guarantee true. `--kill-after` makes the bound real: a parse ignoring SIGTERM would wait forever.
structural_solve() {
  local bin="$1" file="$2" out="$3"
  # Checked HERE and not only in PREPARE's partition, so a future third caller
  # cannot reach the drop by skipping the filter.
  structural_merge_unsafe "$file" && return 1
  timeout --verbose --kill-after=10 60 "$bin" solve -p "$file" >"$out" || return 1
  [[ -s "$out" ]] || return 1
  ! grep -qE '^(<{7}|\|{7}|>{7})([ \t]|$)' "$out"
}

# harness_unwritable_matches PATH… — subset the resolver's own Claude Code process may not write; these route through the sidecar prompt instead. A hook `allow` does NOT outrank the harness refusal, so the sidecar is the ONLY route: PR #3362's resolver run (job 91952141881) left `.pre-commit-config.yaml`'s markers behind reporting "the harness classifies it as a sensitive file and the permission request wasn't granted", after the per-shard PreToolUse hook had already granted that exact absolute path. Override with AUTO_RESOLVE_HARNESS_UNWRITABLE_RE (an ERE per path); set it empty to disable the class.
harness_unwritable_matches() {
  local unwritable="${AUTO_RESOLVE_HARNESS_UNWRITABLE_RE-^(\.claude/|\.pre-commit-config\.yaml$)}" f
  [[ -n "$unwritable" ]] || return 0
  for f in "$@"; do
    [[ "$f" =~ $unwritable ]] && printf '%s\n' "$f"
  done
  return 0
}

# writable_paths MERGE_BASE HEAD — the paths HEAD changed since MERGE_BASE that the resolver may Edit BESIDE its conflicted set, NUL-terminated. Run it INSIDE the merged tree: the file tests below read that tree.
# PROBLEM CLASS — the correct resolution of a conflict lives in a third file: a definition the base moved, a caller the head added. On every verified instance (agent-glovebox#5362) that file was one the head itself changed, so HEAD's own diff bounds the set. Subtracted: a protected path, so a conflicted file's prompt injection cannot reach the supervision stack; a harness-unwritable one, which the model cannot write anyway; a recognized lockfile, which only its lock command may write; a symlink, which is an out-of-tree write primitive; a path absent from the merged tree (HEAD deleted it, or the base renamed it away and git carried the edit across), which no Edit reaches.
# prepare.sh and land.sh both call this with the same two commits and get the same answer, so nothing path-shaped crosses from the untrusted resolve job.
writable_paths() {
  local base="$1" head="$2" f
  local -A lockfile=()
  local -a changed=()
  mapfile -d '' -t changed < <(git diff -z --name-only --diff-filter=d "$base" "$head")
  [[ ${#changed[@]} -gt 0 ]] || return 0
  # Fails CLOSED: a recognizer that crashes would otherwise answer "no lockfile"
  # and hand every lockfile the head changed to the model. Written to a file so
  # the exit status is readable, which a process substitution never gives.
  local recognized
  recognized="$(mktemp)"
  if ! python3 "$AUTO_RESOLVE_DIR/_lockfiles.py" --recognize -- "${changed[@]}" >"$recognized"; then
    echo "auto-resolve: '_lockfiles.py --recognize' failed; widening to no path rather than guessing which are lockfiles." >&2
    rm -f "$recognized"
    return 0
  fi
  while IFS= read -r f; do
    [[ -n "$f" ]] && lockfile["$f"]=1
  done <"$recognized"
  rm -f "$recognized"
  for f in "${changed[@]}"; do
    [[ -z "${lockfile["$f"]:-}" && -f "$f" && ! -L "$f" ]] || continue
    [[ -z "$(protected_matches "$f")" ]] || continue
    [[ -z "$(harness_unwritable_matches "$f")" ]] || continue
    printf '%s\0' "$f"
  done
  return 0
}

# has_marker_triple — true when stdin carries all three marker kinds, each as a
# whole line. The COMPLETE triple is the test, never a single kind.
has_marker_triple() {
  local text kind
  text="$(cat)"
  for kind in '<' '=' '>'; do
    grep -qE "^${kind}{7}([ \t]|\$)" <<<"$text" || return 1
  done
}

# marker_blocks — print each complete `<<<<<<<`…`>>>>>>>` block from stdin,
# fence lines included, as its own NUL-terminated record. A block that never
# reaches a closing `>>>>>>>` line is dropped, since it is not one of the
# resolvable conflicts these markers describe.
#
# A bash state machine, not awk: mawk's regex engine (the default `awk` on
# Ubuntu runners) panics ("values still on machine stack") on an interval
# expression `{7}` inside an alternation group, which is exactly the shape
# `^<{7}([ \t]|$)` needs.
marker_blocks() {
  # Held in variables, not inlined in `[[ =~ ]]`: tree-sitter-bash's grammar
  # (pinned by this repo's own shell-parsing pre-commit hooks) cannot read a
  # `[[:space:]]` bracket expression written inline there.
  local open_re='^<{7}([[:space:]]|$)' close_re='^>{7}([[:space:]]|$)'
  local line block="" in_block=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$in_block" -eq 0 ]]; then
      [[ "$line" =~ $open_re ]] && {
        in_block=1
        block="$line"
      }
      continue
    fi
    block+=$'\n'"$line"
    if [[ "$line" =~ $close_re ]]; then
      printf '%s\0' "$block"
      in_block=0
      block=""
    fi
  done
}

# blocks_subset_of_base BASE_REMOTE_REF PATH — true when every complete marker
# block PATH carries right now already existed, byte-for-byte, in PATH's copy
# at BASE_REMOTE_REF. Comparing whole blocks (not just marker lines) matters
# because the fence lines themselves are fixed boilerplate — `<<<<<<< local`,
# `=======`, `>>>>>>> template` — so a file that legitimately keeps marker text
# as a fixture would make ANY new, unrelated conflict added elsewhere in that
# same file look pre-existing if only the fence lines were compared.
blocks_subset_of_base() {
  local base_ref="$1" path="$2" block b found
  local -a base_blocks=()
  while IFS= read -r -d '' b; do
    base_blocks+=("$b")
  done < <(git cat-file blob "${base_ref}:${path}" 2>/dev/null | marker_blocks)
  while IFS= read -r -d '' block; do
    found=0
    for b in "${base_blocks[@]}"; do
      [[ "$block" == "$b" ]] && {
        found=1
        break
      }
    done
    [[ "$found" -eq 1 ]] || return 1
  done < <(marker_blocks <"$path")
  return 0
}

# committed_marker_paths BASE_REMOTE_REF — tracked paths whose committed content carries conflict markers, e.g. from template-sync.sh's own merge. Excludes currently-unmerged paths and a path whose every current marker block already existed in the base copy (a fixture that gained no new conflict).
committed_marker_paths() {
  local base_ref="$1" f
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    [[ -z "$(git ls-files -u -- "$f")" ]] || continue
    has_marker_triple <"$f" || continue
    blocks_subset_of_base "$base_ref" "$f" && continue
    printf '%s\n' "$f"
    # `git grep` exits 1 when nothing matches, which here means no path carries a
    # marker — the answer, not a failure.
  done < <(git grep -lE "$CONFLICT_MARKER_RE" -- . || true)
  return 0
}

declare -A PATH_FACTS=()
# load_path_facts ROOT BASE_REMOTE_REF OWNED_FILE PATH… — fill PATH_FACTS with
# every per-path verdict `_paths.py` reaches, so the shell reads one answer
# instead of re-deriving each predicate. OWNED_FILE may be empty for a caller
# with no rule table. Callable only mid-merge in ROOT: the binary test compares
# that checkout's HEAD against its MERGE_HEAD.
#
# Fails CLOSED. A classification that could not be read would call every path
# mergeable and hand a binary or a lockfile to the model.
load_path_facts() {
  local root="${1:?load_path_facts: ROOT required}"
  local base_ref="${2:?load_path_facts: BASE_REMOTE_REF required}" owned="$3" out path flags
  shift 3
  PATH_FACTS=()
  [[ $# -gt 0 ]] || return 0
  local -a owned_arg=()
  [[ -z "$owned" ]] || owned_arg=(--owned-file "$owned")
  out="$(mktemp)"
  if ! python3 "$AUTO_RESOLVE_DIR/_paths.py" --root "$root" \
    --base-ref "$base_ref" "${owned_arg[@]}" -- "$@" >"$out"; then
    rm -f "$out"
    echo "auto-resolve: '_paths.py' failed; refusing to partition without a verdict for every conflicted path." >&2
    return 1
  fi
  while IFS= read -r -d "" path && IFS= read -r -d "" flags; do
    PATH_FACTS["$path"]="$flags"
  done <"$out"
  rm -f "$out"
}

# has_fact PATH NAME — true when load_path_facts gave PATH the flag NAME.
has_fact() {
  [[ ",${PATH_FACTS["$1"]:-}," == *",$2,"* ]]
}

# classified PATH — true when load_path_facts answered for PATH at all. A path
# nobody asked about and one holding no flags read the same through has_fact, so
# a caller that must not read "never asked" as "does not hold it" asks here first.
classified() {
  [[ -n "${PATH_FACTS["$1"]+answered}" ]]
}

# free_fragment_path CATEGORY — an unoccupied changelog.d path, $PR_NUMBER then -2, -3, …
free_fragment_path() {
  local id="${PR_NUMBER:-conflict}" suffix=2 candidate="changelog.d/${PR_NUMBER:-conflict}.$1.md"
  while git cat-file -e "HEAD:${candidate}" 2>/dev/null ||
    git cat-file -e "MERGE_HEAD:${candidate}" 2>/dev/null ||
    [[ -e "$candidate" ]]; do
    candidate="changelog.d/${id}-${suffix}.$1.md"
    suffix=$((suffix + 1))
  done
  printf '%s' "$candidate"
}

# split_fragment_collisions — resolve changelog.d/ add/add conflicts by SPLITTING, never by merging into one file: base keeps its path, head moves to a free id.
#
# Reads PATH_FACTS, so the caller runs load_path_facts over every conflicted path
# first. Refuses on a fragment nobody classified: the two `git cat-file blob`
# reads below want stages 2 AND 3, and a one-sided path holds only one of them.
split_fragment_collisions() {
  local f category moved
  local -a fragments=()
  mapfile -d '' -t fragments < <(git diff -z --name-only --diff-filter=U -- 'changelog.d/*')
  [[ ${#fragments[@]} -gt 0 ]] || return 0
  for f in "${fragments[@]}"; do
    [[ "$f" =~ ^changelog\.d/.+\.([a-z]+)\.md$ ]] || continue
    category="${BASH_REMATCH[1]}"
    if ! classified "$f"; then
      echo "auto-resolve: no classification for the conflicted fragment '${f}'; refusing to split without knowing whether both sides added it." >&2
      return 1
    fi
    has_fact "$f" add_add || continue
    moved="$(free_fragment_path "$category")"
    git cat-file blob ":2:$f" >"$moved"
    git cat-file blob ":3:$f" >"$f"
    git add -- "$f" "$moved"
    echo "Fragment id collision on ${f}: the base branch's entry stays there, this PR's moves to ${moved}."
  done
}

# base_tracking_ref BASE_REF — where both steps read the pull request's BASE branch.
#
# INVARIANT — the base side never comes from `origin`. A cross-repository pull
# request checks the HEAD's repository out, so `origin` is the FORK: its copy of
# the base branch is stale, absent, or whatever its author last pushed. land.sh
# gates the untrusted bundle's base-side parent on this ref, so a fork-controlled
# answer would make that gate satisfiable on demand.
base_tracking_ref() {
  printf 'refs/remotes/base/%s' "$1"
}

# fetch_base_ref BASE_REF [--quiet] — update that ref from the BASE repository.
fetch_base_ref() {
  local ref="$1"
  shift
  : "${GH_REPO:?GH_REPO required to fetch the base branch}"
  timeout --kill-after=30 300 git fetch --no-tags "$@" "${GITHUB_SERVER_URL:-https://github.com}/${GH_REPO}.git" \
    "+refs/heads/${ref}:$(base_tracking_ref "$ref")"
}

# reap_group PGID — end anything a finished command left running in its own process group.
#
# PROBLEM CLASS — a command that exits does not take its children with it. A
# derived-file pre-pass that fans generators out and returns on the first failure
# leaves the rest running, and a generator's last act is `git add`: the straggler
# takes `.git/index.lock` seconds later and the NEXT git call in the same step dies
# with "Another git process seems to be running in this repository". A run that hit
# that lost a 40-path resolution nobody could re-run. One signal per group reaches
# everything the command started, because run_settled makes it a group leader.
reap_group() {
  local pgid="$1" i
  kill -0 -- "-${pgid}" 2>/dev/null || return 0
  echo "::warning::the command left processes running after it exited; ending process group ${pgid}, so no straggler of it can write this checkout's index."
  if ! kill -TERM -- "-${pgid}" 2>/dev/null; then
    return 0 # the group drained between the test above and the signal
  fi
  # retry-loop-ok: a poll of the kernel's process table, waiting for a group this step has already signalled to finish dying — there is no command to run a second time, so lib-ci-retry.sh's single-command wrapper cannot express it, and the give-up is an escalation to SIGKILL rather than another attempt
  for ((i = 0; i < 100; i++)); do
    kill -0 -- "-${pgid}" 2>/dev/null || return 0
    sleep 0.1
  done
  if ! kill -KILL -- "-${pgid}" 2>/dev/null; then
    return 0
  fi
}

# require_index_unlocked WHAT — refuse while some process outside this step still holds the index.
#
# A writer killed with SIGTERM removes its own lock; one killed with SIGKILL cannot,
# and a lock whose owner this step cannot name is not one to delete — deleting a LIVE
# writer's lock corrupts the index it is mid-write on. So this waits, then refuses.
require_index_unlocked() {
  local lock i
  lock="$(git rev-parse --git-path index.lock)"
  # retry-loop-ok: a poll of a filesystem predicate another process must clear itself, not a blip retry — nothing here is re-attempted, and the give-up is the refusal below, which is the whole point of waiting rather than deleting the lock
  for ((i = 0; i < 100; i++)); do
    [[ -e "$lock" ]] || return 0
    sleep 0.1
  done
  echo "auto-resolve: ${lock} is still held ten seconds after ${1} finished, so a process this step cannot name is writing this checkout's index." >&2
  echo "Refusing rather than deleting a lock whose owner is unknown. Nothing was landed, and this head's attempt mark is released, so a re-run reaches it." >&2
  return 1
}

# run_settled LABEL OUT_FILE CMD… — run a command the CALLING repository supplied, then leave nothing of it running.
#
# The command's own output goes to OUT_FILE rather than to this step's log, so the
# caller can both print it and read it back for a verdict. `setsid` puts the command
# in a new session, which makes it a process-group leader: `$$` inside that session IS
# the group id, and `exec` keeps that pid for the command itself. It is read from
# inside because setsid execs when it can and forks when it is already a leader, so
# no pid outside the session is reliably the group's. A runner without setsid runs the
# command in this step's own group, where the index barrier below is the only guard.
run_settled() {
  local label="$1" out="$2" rc=0 pgid_file pgid=""
  shift 2
  pgid_file="$(mktemp)"
  if command -v setsid >/dev/null; then
    # shellcheck disable=SC2016  # `$$` must expand inside the new session, not here
    setsid bash -c 'echo "$$" >"$1"; shift; exec "$@"' _ "$pgid_file" "$@" >"$out" 2>&1 || rc=$?
    pgid="$(cat "$pgid_file")"
  else
    "$@" >"$out" 2>&1 || rc=$?
  fi
  rm -f "$pgid_file"
  if [[ -n "$pgid" ]]; then
    reap_group "$pgid"
  fi
  require_index_unlocked "$label" || exit 1
  return "$rc"
}

# pre_pass_could_not_run LOG — print the line showing the pre-pass never RAN, and say so in the exit status.
#
# PROBLEM CLASS — an environment fault and a tree fault leave the same exit status.
# A generator that dies parsing a source file git left conflicted is a TREE fault,
# and FINALIZE re-derives past it. A pre-pass whose own dependencies are not
# installed re-derives NOTHING, so continuing hands a model files no human wrote,
# and reports the crash that follows as a conflict this workflow could not merge.
#
# The patterns live in `_caller_command.py`, which the Python steps read the same
# question from — a package manager's own failure code is not among them, because
# `pnpm` answers ERR_PNPM_INVALID_PACKAGE_JSON both for a manifest git left
# conflicted and for one nobody installed.
pre_pass_could_not_run() {
  python3 "$AUTO_RESOLVE_DIR/_caller_command.py" --could-not-run "$1"
}
