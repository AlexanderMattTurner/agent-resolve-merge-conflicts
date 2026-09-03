#!/usr/bin/env bash
# kcov-exclude: a GitHub Actions step body with a behavioral suite: the suite runs the real
#   script as `bash <script>` against stubbed CLIs on PATH, so the branches are asserted but
#   no run is ever traced.
# Auto-resolve merge conflicts — PREPARE step. Merges the PR base into the checked-out
# head, runs deterministic pre-passes (resolve-generated, the changelog fragment-id split,
# the generated-region re-derivation, then a mergiraf structural merge), then partitions
# what remains so the LLM sees only hand-mergeable text conflicts.
#
# "Conflicted" also covers committed conflict markers git does not report
# (committed_marker_paths in lib.sh).
#
# Outputs: conflict_list (text conflicts for the LLM); deferred_regen
# (rule-owned outputs whose source also conflicted, re-derived after the LLM);
# unresolvable (binary, or a `-merge` file owned by no rule — human only);
# sidecar (conflicts the resolver can read but not write, resolved to a
# scratch file bundle installs); modify_delete (one side deleted the path,
# LLM gives a keep-or-delete verdict); writable_list (unconflicted files this PR
# changed, which a shard may Edit when its resolution reaches into one);
# needs_llm/needs_commit; no_op_head (the attempt mark this run gives back,
# no-op exits only).
#
# A protected-path conflict still goes to the LLM; land flags it for human
# review. The checkout runs persist-credentials: false, so git authenticates
# out-of-band via an HTTP extraheader.
set -euo pipefail

# shellcheck source=.github/resolver/auto-resolve/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# git_auth_header. lib.sh does NOT pull it in, and land.sh reaches it through lib/pr-push.bash
# — this half only fetches and merges, so it takes the auth helper without the push machinery.
# shellcheck source=.github/resolver/lib/git-auth.bash
source "$(dirname "${BASH_SOURCE[0]}")/../lib/git-auth.bash"

: "${BASE_REF:?BASE_REF required}"
: "${HEAD_REF:?HEAD_REF required}"
: "${GITHUB_TOKEN:?GITHUB_TOKEN required}"
out="${GITHUB_OUTPUT:?GITHUB_OUTPUT required}"

git_auth_header "$GITHUB_TOKEN"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

# diff3 markers, without which the structural pre-pass below solves almost
# nothing (lib.sh states why).
configure_merge_conflict_style

# Outranks the consumer's own `.gitattributes`, which this job's
# install-mergiraf.sh has just given a working mergiraf driver to bind to.
override_unsafe_merge_attributes

# From the BASE repository, never from `origin` — lib.sh's base_tracking_ref says
# why. Names the destination explicitly, so the tracking ref always updates
# instead of only opportunistically.
fetch_base_ref "$BASE_REF"
base_ref_name="$(base_tracking_ref "$BASE_REF")"

# Read before the merge can move it: both no-op shapes below compare HEAD to it.
pre_merge_head="$(git rev-parse HEAD)"

# emit_outputs NEEDS_LLM NEEDS_COMMIT [EXTRA...] — every value this step hands on.
#
# PROBLEM CLASS — one question with several answers. Four exits wrote this
# contract by hand, each naming only the keys it held, so a key added at one read
# as empty at the other three and no reader could tell that from a genuinely
# empty list. Each array takes `:-` because three of those exits run before the
# partition declares them, and `set -u` aborts on an undeclared one.
emit_outputs() {
  local needs_llm=$1 needs_commit=$2 extra
  shift 2
  {
    echo "needs_llm=${needs_llm}"
    echo "needs_commit=${needs_commit}"
    echo "conflict_list=${llm_list[*]:-}"
    echo "deferred_regen=${deferred_regen[*]:-}"
    echo "deferred_lockfiles=${builtin_deferred[*]:-}"
    echo "modify_delete=${modify_delete[*]:-}"
    echo "sidecar=${sidecar[*]:-}"
    echo "writable_list=${writable[*]:-}"
    echo "unresolvable=${unresolvable[*]:-}"
    for extra in "$@"; do echo "$extra"; done
  } >>"$out"
}

# no_op_exit REASON — end the run having changed nothing, LOUDLY. Discovery
# reported this PR conflicted, but prepare found nothing to resolve, so this
# hands back the attempt mark (`no_op_head`) for a later scan to retry, rather
# than suppress the PR until the mark's TTL.
no_op_exit() {
  echo "::warning::Auto-resolve made no change to PR #${PR_NUMBER:-?} (${HEAD_REF}): $1. Discovery reported this PR conflicted, so the two disagree — this run resolved nothing, so it releases ${pre_merge_head}'s attempt mark and a later scan may retry it."
  emit_outputs false false "no_op_head=${pre_merge_head}"
  exit 0
}

# install_merged_node_deps — node_modules for the MERGED tree, not the head's.
# The job installs from the PR HEAD's manifests before the merge, so a
# dependency the base adopted is absent while generators run, and
# `resolve-generated` cannot re-derive artifacts that conflict when sources
# move. `--frozen-lockfile` has no fallback: an install allowed to write
# pnpm-lock.yaml would author bytes no rule derives; on failure, warn and
# continue on the head's node_modules.
install_merged_node_deps() {
  # An untrusted head's lockfile names the registries and tarballs this install
  # would fetch, in the job holding every model credential. The resolve job
  # installs no pnpm for such a run either; this refusal is what makes that a
  # decision rather than an accident.
  [[ "${AUTO_RESOLVE_UNTRUSTED_HEAD:-}" != "true" ]] || return 0
  [[ -f package.json ]] || return 0
  if git diff --quiet "$pre_merge_head" -- \
    package.json '*/package.json' pnpm-workspace.yaml pnpm-lock.yaml; then
    echo "The merge left the node manifests unchanged — keeping the node_modules installed from ${HEAD_REF}."
    return 0
  fi
  echo "The merge changed the node manifests — reinstalling node_modules from the merged tree."
  # echo-fallback-ok: a GitHub warning annotation, not a value.
  pnpm install --frozen-lockfile --ignore-scripts ||
    echo "::warning::pnpm install against the merged manifests failed; continuing on the node_modules installed from ${HEAD_REF} — a generator importing a dependency the base added will fail below."
}

# The CALLING repository's derived-file resolver, from the workflow's
# `resolver-mjs` input, alongside the command that runs it from the caller's own
# tree (`pre-pass-command`). Both empty is a caller with no generated files: it
# has no derived-file machinery to run and no ownership table to consult, so
# every conflict below is a hand-written one. Never a guessed default — a path
# guessed wrong re-derives nothing and reports that as "nothing to re-derive".
resolver_mjs="${AUTO_RESOLVE_RESOLVER_MJS:-}"
# PROBLEM CLASS — the resolver runs from the TRUSTED BASE clone, which nobody installs
# dependencies into, so a package it reaches through `createRequire` answers `Cannot find
# module` and each reader takes that for a fault of its own: this script exits 78, and
# bundle's self-review leaves the resolution unverified. NODE_PATH aims those CJS lookups
# at the merged worktree instead, ahead of whatever a caller's setup already put there.
owned_file=""
pre_pass="${AUTO_RESOLVE_PRE_PASS:-}"
post_merge_check="${AUTO_RESOLVE_POST_MERGE_CHECK:-}"
pre_pass_argv=()
if [[ -n "$pre_pass" ]]; then
  # Written to a file, the way the `--owned` read below is, because `mapfile`
  # returns 0 whatever the process substitution did and `pipefail` does not
  # reach inside `< <(…)`. An unbalanced quote would otherwise leave this array
  # EMPTY, `run_settled` would exec nothing, and a pre-pass that never ran would
  # report success — the generated files then reach the model unrederived.
  pre_pass_argv_raw="$(mktemp)"
  python3 "$AUTO_RESOLVE_DIR/_caller_command.py" --argv "$pre_pass" >"$pre_pass_argv_raw" || {
    rm -f "$pre_pass_argv_raw"
    echo "auto-resolve/prepare: could not split \`pre-pass-command\` into an argv; refusing rather than running nothing and calling it a re-derivation." >&2
    exit 78 # EXIT_MISCONFIGURED — the caller's wiring, not this tree's conflict.
  }
  mapfile -d '' -t pre_pass_argv <"$pre_pass_argv_raw"
  rm -f "$pre_pass_argv_raw"
  [[ ${#pre_pass_argv[@]} -gt 0 ]] || {
    echo "auto-resolve/prepare: \`pre-pass-command\` is set but split to no arguments." >&2
    exit 78
  }
fi

# Same shape as the mergiraf pre-flight below, and here each saves a whole billed
# resolution: `bundle.py` runs both of these AFTER the model has resolved every
# shard, and a binary the job never installs refuses there with nothing landed.
# Skipped for a fork head, the one run `bundle.py` empties both of its copies for.
# Called as a plain command, never in `$(…)`, so this `exit` reaches the caller.
refuse_a_caller_tool_the_runner_lacks() {
  local input="$1" cmd="$2" lost="$3" bin
  [[ -n "$cmd" && "${AUTO_RESOLVE_UNTRUSTED_HEAD:-}" != "true" ]] || return 0
  # Split the way the command is RUN, not on the first space: a quoted program
  # holding whitespace named a different binary to this pre-flight than to
  # `run_settled` below, so the check passed and the run then found nothing.
  bin="$(python3 "$AUTO_RESOLVE_DIR/_caller_command.py" --program "$cmd")"
  command -v "$bin" >/dev/null && return 0
  echo "auto-resolve/prepare: the \`${input}\` binary '${bin}' is not on this runner's PATH — install it in the calling workflow, or clear \`${input}\`; refusing before this run buys a resolution it could not ${lost}." >&2
  exit 78 # EXIT_MISCONFIGURED — the caller's wiring, not this tree's conflict.
}
refuse_a_caller_tool_the_runner_lacks pre-pass-command "$pre_pass" re-derive
refuse_a_caller_tool_the_runner_lacks post-merge-check-command "$post_merge_check" check

merge_rc=0
git merge --no-edit "$base_ref_name" || merge_rc=$?
install_merged_node_deps

# ASKED AFTER THE REINSTALL ABOVE, and after the merge that made it necessary.
# The resolver imports the merged tree's packages, so a dependency the BASE
# branch adopted is absent from the head's node_modules and this read fails on a
# perfectly mergeable pull request. Nothing between the merge and here consumes
# the answer — the first reader is the lockfile router below.
if [[ -n "$resolver_mjs" ]]; then
  export NODE_PATH="${PWD}/node_modules${NODE_PATH:+:${NODE_PATH}}"
  # ONE ownership answer for the whole run, asked of the TRUSTED-BASE resolver
  # under `node` (`pnpm` parses package.json, which mid-merge can carry markers;
  # `--owned` parses no manifest). Fails CLOSED: an oracle answering "nothing is
  # owned" when broken misroutes exactly the paths it exists to route — a
  # caller-owned lockfile would go to this resolver's built-in rules, and a
  # generated file would reach the model instead of its generator.
  owned_file="$(mktemp)"
  node "$resolver_mjs" --owned >"$owned_file" || {
    echo "auto-resolve/prepare: 'node ${resolver_mjs} --owned' failed." >&2
    echo "Without an ownership answer, a re-derivable lockfile reads as unmergeable and goes to a human." >&2
    echo "This step refuses to route or partition instead." >&2
    exit 1
  }
fi

# INVARIANT — a recognized lockfile both sides changed is never left as git
# merged it, and this pass runs before any textual, structural or LLM one reads
# its bytes. The trigger is "both parents changed it", not "git could not merge
# it": without a `-merge` attribute git line-merges the two sides cleanly, so no
# path conflicts and the branch carries entries neither manifest produces.
_lockfiles_py="$(dirname "${BASH_SOURCE[0]}")/_lockfiles.py"
builtin_deferred=()
builtin_refused=()
lockfile_candidates=()
if [[ "${AUTO_RESOLVE_UNTRUSTED_HEAD:-}" == "true" ]]; then
  # A fork head's manifest names the build backends a regen command would run,
  # in the job holding every credential — the same reason PRE_PASS is emptied
  # for such a run. Recognizing a lockfile touches no filesystem and runs no
  # tool, so it is safe here; regenerating one is not.
  mapfile -t fork_conflicts < <(git diff --name-only --diff-filter=U)
  if [[ ${#fork_conflicts[@]} -gt 0 ]]; then
    mapfile -t builtin_refused < <(
      python3 "$_lockfiles_py" --recognize -- "${fork_conflicts[@]}"
    )
  fi
  for f in "${builtin_refused[@]}"; do
    echo "::error::the lockfile '${f}' conflicted on a fork head — this job runs no lock command over a fork's manifest, so it hands off rather than merging it as text."
  done
else
  merge_base_sha="$(git merge-base "$pre_merge_head" "$base_ref_name")"
  declare -A base_changed=()
  while IFS= read -r f; do
    [[ -n "$f" ]] && base_changed["$f"]=1
  done < <(git diff --name-only "$merge_base_sha" "$base_ref_name")
  while IFS= read -r f; do
    [[ -n "$f" && -n "${base_changed["$f"]:-}" ]] && lockfile_candidates+=("$f")
  done < <(git diff --name-only "$merge_base_sha" "$pre_merge_head")
fi
if [[ ${#lockfile_candidates[@]} -gt 0 ]]; then
  # The common ancestor, not either merged side: seeding a relock from it keeps
  # the relock from picking up either side's own transitive bumps, so the
  # regenerated lockfile's delta stays just what the merged manifests forced.
  route_args=(--seed-ref "$merge_base_sha")
  [[ -z "$owned_file" ]] || route_args+=(--owned-file "$owned_file")
  while IFS= read -r f; do
    [[ -n "$f" ]] && route_args+=(--manifest-conflicted "$f")
  done < <(git diff --name-only --diff-filter=U)
  # Written to a file rather than read from a process substitution: the latter
  # gives no way to check the command's own exit status, so a router crash
  # (a traceback, a bad argv) would silently route nothing and leave every
  # candidate's bytes as git merged them — the outcome this pass exists to stop.
  route_output="$(mktemp)"
  python3 "$_lockfiles_py" --route --root "$PWD" "${route_args[@]}" \
    -- "${lockfile_candidates[@]}" >"$route_output"
  route_rc=$?
  if [[ "$route_rc" -ne 0 ]]; then
    echo "auto-resolve/prepare: '_lockfiles.py --route' exited ${route_rc}; refusing to route lockfiles without a verdict." >&2
    exit 1
  fi
  while IFS=$'\t' read -r verdict path reason; do
    case "$verdict" in
    regenerated)
      echo "Regenerated the conflicted lockfile '${path}' from its merged manifest."
      # `reason` here is the touched-path list (the lockfile plus any declared
      # co-output — go.sum's generator legitimately rewrites go.mod too).
      # shellcheck disable=SC2086
      git add -- ${reason}
      ;;
    caller-owned)
      echo "Lockfile '${path}' is owned by this repository's own regeneration rule."
      ;;
    deferred)
      builtin_deferred+=("$path")
      ;;
    refused)
      builtin_refused+=("$path")
      echo "::error::the lockfile '${path}' cannot be regenerated (${reason}), and a textual merge of a lockfile is silent corruption. Leaving it for a human."
      ;;
    *)
      # A verdict this script does not know is a contract break between it and
      # _lockfiles.py, and silently ignoring one leaves the lockfile as git
      # merged it — the outcome this whole pass exists to prevent.
      echo "auto-resolve/prepare: unknown lockfile verdict '${verdict}' for '${path}'." >&2
      exit 1
      ;;
    esac
  done <"$route_output"
  rm -f "$route_output"
  # A caller-owned lockfile git merged CLEANLY never reaches the pre-pass below,
  # which only runs on the conflicted path. Re-derive here so the merge cannot
  # carry line-merged bytes.
  if [[ "$merge_rc" -eq 0 && -n "$pre_pass" ]]; then
    clean_prepass_log="$(mktemp)"
    # echo-fallback-ok: a GitHub warning annotation, not a value. The pre-pass is
    # advisory here — bundle re-runs it and verifies the bytes byte-for-byte.
    run_settled "the derived-file pre-pass" "$clean_prepass_log" "${pre_pass_argv[@]}" ||
      echo "::warning::the derived-file pre-pass exited non-zero re-deriving a cleanly-merged lockfile; the paths it owns keep the bytes git merged."
    cat "$clean_prepass_log"
    rm -f "$clean_prepass_log"
    git add -A
  fi
fi
# Seeded here rather than at the partition below: the exit that follows reports
# it, and the partition only appends.
unresolvable=("${builtin_refused[@]}")
if [[ ${#builtin_refused[@]} -gt 0 && "$merge_rc" -eq 0 ]]; then
  echo "Unresolvable lockfile(s) '${builtin_refused[*]}' — handing off rather than pushing bytes no lock command produces."
  emit_outputs false false
  exit 0
fi

if [[ "$merge_rc" -eq 0 ]]; then
  merged_head="$(git rev-parse HEAD)"
  if [[ "$merged_head" == "$pre_merge_head" ]]; then
    # `Already up to date`: the base is an ancestor of the head.
    no_op_exit "${BASE_REF} is already contained in ${HEAD_REF}, so there was no merge to make"
  fi
  if git merge-base --is-ancestor "$pre_merge_head" "$base_ref_name"; then
    # A fast-forward: pushing HEAD now would replace the PR branch with base.
    no_op_exit "${HEAD_REF} is already contained in ${BASE_REF}, so the merge fast-forwarded and there is nothing of this PR's own to push"
  fi
  # git merged cleanly, yet DISCOVER reported this PR conflicted — commonest
  # when the base renamed a file the PR modified, since rename detection carries
  # the edit across. The merge commit IS the resolution and pushing it is what
  # the API re-reads; dropping it strands the PR, because this run has already
  # marked the head attempted and no scan re-reaches it before the mark's TTL.
  echo "No conflicts merging ${BASE_REF} into ${HEAD_REF}, but discovery reported this PR conflicted — pushing the clean merge to clear it."
  {
    echo "needs_llm=false"
    echo "needs_commit=true"
  } >>"$out"
  exit 0
fi

# Deterministic pre-pass: re-derive + stage every conflicted derived file
# whose source merged cleanly. Non-fatal: FINALIZE re-runs and verifies it.
if [[ -n "$pre_pass" || -n "$resolver_mjs" ]]; then
  prepass_rc=0
  prepass_log="$(mktemp)"
  if [[ -n "$pre_pass" ]]; then
    run_settled "the derived-file pre-pass" "$prepass_log" "${pre_pass_argv[@]}" || prepass_rc=$?
    cat "$prepass_log"
  else
    prepass_rc=1 # nothing to run from the PR's tree; go straight to the staged copy
  fi
  if [[ "$prepass_rc" -ne 0 && -n "$resolver_mjs" ]]; then
    # PROBLEM CLASS — a tool that rewrites a conflicted tree is itself a file in that
    # tree, so a merge whose conflict set holds the caller's resolver (or a module it
    # imports, or the package.json pnpm resolves it through) leaves the line above
    # unable to parse it and re-deriving NOTHING. Retry with the copy from the
    # trusted base; --root aims it here, and FINALIZE's --verify bounds the gap.
    echo "::warning::the derived-file pre-pass exited ${prepass_rc} running the PR's own copy; retrying with the trusted-base copy in case the resolver itself is conflicted."
    prepass_rc=0
    run_settled "the trusted-base derived-file pre-pass" "$prepass_log" \
      node "$resolver_mjs" "--root=$PWD" || prepass_rc=$?
    cat "$prepass_log"
  fi
  if [[ "$prepass_rc" -ne 0 ]]; then
    # A pre-pass that could not START re-derived nothing, so every derived file in
    # this merge still holds bytes no generator produced. Refusing here costs a
    # runner minute; continuing spends a model on files no human wrote and then
    # reports the crash that follows as a conflict this workflow could not merge.
    if env_fault="$(pre_pass_could_not_run "$prepass_log")"; then
      echo "auto-resolve/prepare: the derived-file pre-pass exited ${prepass_rc} without running the generators: ${env_fault}" >&2
      echo "That names this JOB's environment, not a conflict in this pull request, so no derived file was re-derived." >&2
      echo "Install what the pre-pass needs in the calling workflow — the \`setup-command\` input, or whatever install its generators run — and re-run. Nothing was landed, and this head's attempt mark is released, so a re-run reaches this same head." >&2
      exit 78 # EXIT_MISCONFIGURED — the caller's environment, not this tree's conflict.
    fi
    echo "::warning::the derived-file pre-pass exited ${prepass_rc} (a generator crashed on a conflicted source, the resolver would not load, or an output still carries markers); continuing — FINALIZE re-runs it and verifies generated content byte-for-byte."
  fi
  rm -f "$prepass_log"
fi

# The classification the fragment split below reads, from the same `_paths.py`
# the partition further down reads. Nothing between `git merge` above and here
# moves HEAD, MERGE_HEAD or the base ref, so one answer serves both. Fails
# closed under `set -e`.
mapfile -d '' -t pre_pass_conflicts < <(git diff -z --name-only --diff-filter=U)
[[ ${#pre_pass_conflicts[@]} -eq 0 ]] ||
  load_path_facts . "$base_ref_name" "$owned_file" "${pre_pass_conflicts[@]}"

# Second deterministic pre-pass: a changelog fragment id both sides guessed
# has one correct resolution (keep both files, distinct ids) an LLM would miss.
split_fragment_collisions

# Third deterministic pre-pass: a conflict INSIDE a `BEGIN GENERATED` region of
# a hand-written file. resolve-generated above owns whole derived files only, so
# a spliced region reaches the LLM to be merged by hand — and the model does not
# always merge it. This runs the generator the region's own marker names, before
# the conflict list is read below. Non-fatal for the same reason resolve-generated
# is: a file this pass cannot finish keeps the text git wrote and reaches the LLM
# exactly as it did before the pass existed.
region_rc=0
region_defer_file="$(mktemp)"
REGION_DEFER_FILE="$region_defer_file" python3 "$(dirname "${BASH_SOURCE[0]}")/regen_marked_regions.py" || region_rc=$?
if [[ "$region_rc" -ne 0 ]]; then
  echo "::warning::the generated-region pre-pass exited ${region_rc}; continuing — every conflict it did not stage goes to the LLM."
fi
# A region whose generator cannot read a still-conflicted tree. bundle re-runs
# that pass after the LLM resolves the rest, so the path is kept away from the
# LLM here for the reason a rule-owned generated file is: neither side of a
# derived region is the answer.
declare -A region_deferred=()
while IFS= read -r f; do
  [[ -n "$f" ]] && region_deferred["$f"]=1
done <"$region_defer_file"

# What a PRIOR round of this same head already resolved, installed before the
# conflict list is read: a carried path is staged, so it leaves the set this run
# buys and the window goes to the remainder. apply-salvage.py refuses unless
# both its pins match, and a refusal leaves the merge exactly as git wrote it.
if [[ -n "${SALVAGE_DIR:-}" ]]; then
  MERGE_BASE="$(git merge-base HEAD MERGE_HEAD)" python3 "$(dirname "${BASH_SOURCE[0]}")/apply-salvage.py"
fi

# Give a missed rename the three-way merge git would have done: a launcher left at
# the old path defeats rename detection, so the other side's edits to that path
# land nowhere. A clean port resolves the destination; a conflicting one leaves it
# unmerged, so it enters the conflict list below like any other path. Non-fatal,
# and a refusal restores the index, so the merge is then as git wrote it.
port_rc=0
python3 "$(dirname "${BASH_SOURCE[0]}")/_relocation_port.py" --root "$PWD" || port_rc=$?
if [[ "$port_rc" -ne 0 ]]; then
  echo "::warning::the relocation port exited ${port_rc}; continuing — every conflict it did not port goes to the LLM as before."
fi

# Take the formatter's padding out of a conflicted markdown table and re-merge the
# rows: a table prettier pads to fixed column widths puts a three-row disagreement
# in front of the model as eighty rewritten rows. Non-fatal — an unnarrowed file
# reaches the LLM as git wrote it. NARROW_SKIP_FILE hands over the deferred
# generated regions above, which bundle's own generator owns rather than a merge.
narrow_rc=0
NARROW_SKIP_FILE="$region_defer_file" python3 "$(dirname "${BASH_SOURCE[0]}")/narrow_padded_tables.py" || narrow_rc=$?
if [[ "$narrow_rc" -ne 0 ]]; then
  echo "::warning::the table-padding pre-pass exited ${narrow_rc}; continuing — every conflict it did not narrow goes to the LLM as before."
fi
rm -f "$region_defer_file"

# Last deterministic pre-pass: a path BOTH sides deleted. git leaves stage 1
# alone and writes NO file, so nothing is left to edit and nothing is left to
# keep — the resolution is the deletion git already holds, and `git rm` stages
# it. Handing one to a human costs them a mechanical `git rm`; leaving it in the
# list below opens a marker prompt about a file the worktree does not hold.
# Non-fatal, like the passes above: a path this cannot stage keeps its unmerged
# state and the partition routes it to a human.
stage_both_deleted_paths() {
  local f
  local -a unresolved=() staged=()
  mapfile -d '' -t unresolved < <(git diff -z --name-only --diff-filter=U)
  [[ ${#unresolved[@]} -gt 0 ]] || return 0
  # Classified here rather than read from the pass above the fragment split: the
  # relocation port can leave a path unmerged that was not before it ran, and an
  # unclassified path reads through `has_fact` as one git did not both-delete.
  load_path_facts . "$base_ref_name" "$owned_file" "${unresolved[@]}" || return 1
  for f in "${unresolved[@]}"; do
    has_fact "$f" both_deleted || continue
    git rm -q -f -- "$f" || {
      echo "::warning::both sides deleted '${f}' and 'git rm' would not stage that deletion; leaving it for a human."
      continue
    }
    staged+=("$f")
  done
  if [[ ${#staged[@]} -gt 0 ]]; then
    echo "Both sides deleted ${#staged[@]} path(s); staging the deletion git already holds: ${staged[*]}"
  fi
}
stage_both_deleted_paths

mapfile -t conflicts < <(git diff --name-only --diff-filter=U)
declare -A unmerged=()
for f in "${conflicts[@]}"; do unmerged["$f"]=1; done

# The pre-pass generators also rewrite their UNOWNED splice outputs in the
# working tree. Restore those to the merged index state so bundle.py's
# out-of-set guard sees only the LLM's edits.
while IFS= read -r f; do
  [[ -z "$f" || -n "${unmerged["$f"]:-}" ]] && continue
  git checkout -- "$f"
done < <(git diff --name-only)

# Conflicts git does NOT report: markers a tool committed as ordinary file
# content. Read after the pre-passes, so the scan never mistakes regen noise for damage.
mapfile -t marker_damaged < <(committed_marker_paths "$base_ref_name")
if [[ ${#marker_damaged[@]} -gt 0 ]]; then
  echo "Committed conflict marker(s) in ${#marker_damaged[@]} file(s) git reports as unconflicted: ${marker_damaged[*]}"
fi

if [[ ${#conflicts[@]} -eq 0 && ${#marker_damaged[@]} -eq 0 ]]; then
  echo "All conflicts resolved deterministically — committing without Claude."
  emit_outputs false true
  exit 0
fi

# ONE classification for every path the partition below judges, so a pass added
# later reads an answer instead of re-deriving a predicate and disagreeing with
# the passes already here.
load_path_facts . "$base_ref_name" "$owned_file" "${conflicts[@]}" "${marker_damaged[@]}"

# Partition. `_conflict_set.py` routes every conflicted path and records the
# claim, so each leaves with exactly one disposition. The buckets below are that
# router's answer, read rather than re-derived: the chain of shell tests this
# replaces had to agree with `route` by hand, and a disagreement between them
# routed one path two ways in silence.
llm_list=()
deferred_regen=()
modify_delete=()
structural_candidates=()
# A path git merged under a NAMED merge driver. Git ran that driver and wrote
# what it produced, so the file already holds an answer and any markers in it
# are the driver's own. It reaches the model like other text conflicts, and it
# skips the structural pre-pass alone: `mergiraf solve` re-merges a path from its
# three index stages, which discards the driver's output and reports no loss.
driver_bound=()
ledger_json="${RUNNER_TEMP:-/tmp}/auto-resolve-ledger.json"
ledger_seen="$(mktemp)"
printf '%s\0' "${conflicts[@]+"${conflicts[@]}"}" >"$ledger_seen"
route_args=(--base-ref "$base_ref_name" --ledger-out "$ledger_json" --compare-to "$ledger_seen")
[[ -z "$owned_file" ]] || route_args+=(--owned-file "$owned_file")
for f in "${builtin_refused[@]}"; do route_args+=(--lockfile-refused "$f"); done
for f in "${builtin_deferred[@]}"; do route_args+=(--lockfile-deferred "$f"); done
for f in "${!region_deferred[@]}"; do route_args+=(--region-deferred "$f"); done
partition_out="$(mktemp)"
if ! python3 "$AUTO_RESOLVE_DIR/_conflict_set.py" "${route_args[@]}" >"$partition_out"; then
  rm -f "$partition_out" "$ledger_seen"
  echo "auto-resolve/prepare: '_conflict_set.py' failed; refusing to partition without a disposition for every conflicted path." >&2
  exit 1
fi
rm -f "$ledger_seen"
routed=0
while IFS= read -r -d "" f && IFS= read -r -d "" bucket; do
  routed=$((routed + 1))
  case "$bucket" in
  # A recognized lockfile the routing pass refused or deferred is already in an
  # array of its own, and reaches neither mergiraf nor the model: a hand or
  # structural resolution of one is a guess at what the lock command produces.
  skip) ;;
  deferred_regen) deferred_regen+=("$f") ;;
  unresolvable) unresolvable+=("$f") ;;
  modify_delete)
    modify_delete+=("$f")
    llm_list+=("$f")
    ;;
  driver_bound)
    driver_bound+=("$f")
    llm_list+=("$f")
    ;;
  structural)
    structural_candidates+=("$f")
    llm_list+=("$f")
    ;;
  *)
    # A bucket this script does not know is a contract break with
    # _conflict_set.py, and ignoring one silently lands the bytes git merged
    # with no pass having judged them.
    echo "auto-resolve/prepare: unknown partition bucket '${bucket}' for '${f}'." >&2
    exit 1
    ;;
  esac
done <"$partition_out"
rm -f "$partition_out"
# The router reads `git ls-files -u`; `conflicts` reads `git diff
# --diff-filter=U`, which C-quotes a name holding whitespace or a quote. A short
# count is a path one of the two readers cannot name, and dropping it silently
# is exactly what this set exists to stop.
if [[ "$routed" -ne ${#conflicts[@]} ]]; then
  echo "auto-resolve/prepare: routed ${routed} path(s) but git reports ${#conflicts[@]} conflicted; refusing to partition a set the two readers disagree about." >&2
  exit 1
fi
echo "Routed ${routed} conflicted path(s); ledger: ${ledger_json}"
if [[ ${#driver_bound[@]} -gt 0 ]]; then
  echo "Keeping the structural pre-pass off ${#driver_bound[@]} path(s) a named merge driver already merged: ${driver_bound[*]}"
fi

# An unresolvable path ALONE aborts: nothing else needs attention, so the full stop costs
# nothing. Beside other work, each unresolvable path keeps HEAD_REF's own content instead,
# because a merge commit cannot be created with a path left unmerged. That drops the base's
# edit to that one file, and land re-derives the drop from the pushed blobs and flags it —
# this step's own claim about it must not be the only record.
if [[ ${#unresolvable[@]} -gt 0 ]]; then
  if [[ ${#llm_list[@]} -eq 0 && ${#deferred_regen[@]} -eq 0 ]]; then
    echo "Unmergeable conflict(s) '${unresolvable[*]}' — no textual resolution exists; handing off to a human."
    emit_outputs false false
    exit 0
  fi
  echo "Unmergeable conflict(s) '${unresolvable[*]}' — no textual resolution exists, but other conflicts in this PR do; keeping ${HEAD_REF}'s own content there so the merge can still be committed."
  for f in "${unresolvable[@]}"; do
    if [[ -n "$(git ls-files -u -- "$f")" ]]; then
      # A modify/delete-shaped unresolvable path (deleted on HEAD_REF, edited on
      # the base) has no `ours` stage — is_unmergeable is checked before
      # is_modify_delete above, so this class never reaches that partition.
      # `HEAD_REF`'s own content there is its deletion, so stage that instead.
      if git checkout --ours -- "$f" 2>/dev/null; then
        git add -- "$f"
      else
        git rm -q -f -- "$f"
      fi
    else
      # A recognized lockfile git already merged CLEANLY (no `-merge` attribute
      # in this caller's tree) is not left unmerged, so `--ours` has no stage to
      # read here — falling through to `git rm -f` would DELETE the lockfile
      # rather than keep HEAD_REF's content. Restore it from the pre-merge head.
      git checkout "$pre_merge_head" -- "$f"
      git add -- "$f"
    fi
  done
fi

# Fourth deterministic pre-pass: a syntax-aware structural merge. mergiraf
# re-merges each remaining conflict from its markers; a file it FULLY solves
# (exit 0, marker-free) is staged here and skips the LLM. A missing binary dies
# loud (override MERGIRAF_BIN). Modify/delete paths are excluded: already
# marker-free, so one fed here would call the surviving side a structural solve.
if [[ ${#structural_candidates[@]} -gt 0 ]]; then
  mergiraf_bin="${MERGIRAF_BIN:-mergiraf}"
  command -v "$mergiraf_bin" >/dev/null || {
    echo "auto-resolve/prepare: '${mergiraf_bin}' not found on PATH — the resolve job installs it via install-mergiraf.sh; refusing to silently skip the structural pre-pass." >&2
    exit 1
  }
  mergiraf_scratch="$(mktemp -d)"
  trap 'rm -rf "$mergiraf_scratch"' EXIT
  structurally_solved=()
  still_conflicted=()
  structurally_skipped=()
  for f in "${structural_candidates[@]}"; do
    # Partitioned here as well as refused inside structural_solve, so the run
    # log names this set rather than folding it into "mergiraf left N".
    if structural_merge_unsafe "$f"; then
      structurally_skipped+=("$f")
      still_conflicted+=("$f")
      continue
    fi
    # lib.sh owns the "fully solved" test, shared with real-merge-probe.sh.
    if structural_solve "$mergiraf_bin" "./${f}" "$mergiraf_scratch/solved"; then
      cat "$mergiraf_scratch/solved" >"$f"
      git add "./${f}"
      structurally_solved+=("$f")
    else
      still_conflicted+=("$f")
    fi
  done
  # Logged: solved / (solved + left) over real resolves is this pass's worth.
  if [[ ${#structurally_skipped[@]} -gt 0 ]]; then
    echo "mergiraf skipped ${#structurally_skipped[@]} conflict(s) whose type it drops content on (see lib.sh structural_merge_unsafe): ${structurally_skipped[*]}"
  fi
  if [[ ${#still_conflicted[@]} -gt 0 ]]; then
    echo "mergiraf left ${#still_conflicted[@]} conflict(s) for the LLM: ${still_conflicted[*]}"
  fi
  if [[ ${#structurally_solved[@]} -gt 0 ]]; then
    echo "mergiraf structurally resolved ${#structurally_solved[@]} conflict(s): ${structurally_solved[*]}"
  fi
  # Rebuilt rather than filtered, so it names exactly what mergiraf left. Every
  # partition that skipped the structural pass has to be listed again here: a
  # driver-bound path is in none of the three arrays this loop wrote, so leaving
  # it out drops it from `conflict_list` whenever any OTHER conflict reached
  # mergiraf, and its markers land on the branch with no pass having read them.
  llm_list=("${modify_delete[@]}" "${driver_bound[@]}" "${still_conflicted[@]}")
fi

# Marker-damaged paths join the partition last, after mergiraf rewrites `llm_list`. A
# rule-owned one stays AWAY FROM THE LLM: hand-editing markers out of a derived file yields
# bytes BUNDLE's `--verify` refuses, and resolve-generated only re-derives paths git reports
# unmerged. The rest carry ordinary marker text and go to the ordinary marker prompt, not to
# mergiraf, whose `solve` expects markers git wrote.
for f in "${marker_damaged[@]}"; do
  if has_fact "$f" generated_owned; then
    deferred_regen+=("$f")
  else
    llm_list+=("$f")
  fi
done

# A conflict in a protected path (set in lib.sh) still goes to the LLM; land
# flags it for human review in the comment on the pushed resolution. Reported
# here for the log only — land re-derives its own copy from the verified diff.
mapfile -t protected_hits < <(protected_matches "${conflicts[@]}" "${marker_damaged[@]}")
if [[ ${#protected_hits[@]} -gt 0 ]]; then
  echo "Conflict in protected path(s) '${protected_hits[*]}' — land will flag for human review; still auto-resolving."
fi

# The files this PR itself changed, which a shard may also Edit when the correct
# resolution of its conflict reaches into one (lib.sh's writable_paths says why
# that bound and no wider). Every conflicted or rule-owned path is taken out:
# each of those has its own partition above, and this list must not reopen one.
declare -A not_widenable=()
for f in "${conflicts[@]}" "${marker_damaged[@]}" "${deferred_regen[@]}" "${unresolvable[@]}" "${builtin_deferred[@]}"; do
  not_widenable["$f"]=1
done
writable=()
merge_base_now="$(git merge-base HEAD MERGE_HEAD)"
widenable=()
while IFS= read -r -d '' f; do
  [[ -n "${not_widenable["$f"]:-}" ]] || widenable+=("$f")
done < <(writable_paths "$merge_base_now" HEAD)
[[ ${#widenable[@]} -eq 0 ]] ||
  load_path_facts . "$base_ref_name" "$owned_file" "${widenable[@]}"
for f in "${widenable[@]+"${widenable[@]}"}"; do
  has_fact "$f" generated_owned && continue
  # `writable_list` is whitespace-separated, so a path carrying whitespace
  # cannot cross the step boundary whole: fanout would read it as fragments
  # and refuse the whole run over a file that never conflicted.
  if [[ "$f" =~ [[:space:]] ]]; then
    echo "Leaving '${f}' out of the writable set: its name carries whitespace, which the step outputs cannot carry."
    continue
  fi
  writable+=("$f")
done
if [[ ${#writable[@]} -gt 0 ]]; then
  echo "The resolver may also edit ${#writable[@]} file(s) this PR changed, when a resolution reaches into one: ${writable[*]}"
fi

# A conflict the resolver cannot WRITE still gets resolved: the harness refuses
# Edit/Write on its own hook/grant configuration (lib.sh lists the set) but
# reads it freely. These get the SIDECAR prompt: the shard emits the resolved
# file to a scratch path bundle.py installs. Modify/delete paths are excluded
# — they already use `git add`/`git rm` — so each path takes one prompt.
sidecar=()
if [[ ${#llm_list[@]} -gt 0 ]]; then
  _md_set=" ${modify_delete[*]:-} "
  markered=()
  for f in "${llm_list[@]}"; do
    [[ "$_md_set" == *" $f "* ]] || markered+=("$f")
  done
  [[ ${#markered[@]} -eq 0 ]] ||
    mapfile -t sidecar < <(harness_unwritable_matches "${markered[@]}")
fi
if [[ ${#sidecar[@]} -gt 0 ]]; then
  echo "Conflict(s) '${sidecar[*]}' sit where the resolver cannot write in place — each is resolved through a scratch file bundle installs."
fi

needs_llm=false
[[ ${#llm_list[@]} -gt 0 ]] && needs_llm=true
echo "Handing ${#llm_list[@]} source conflict(s) to Claude: ${llm_list[*]:-<none>}"
if [[ ${#deferred_regen[@]} -gt 0 ]]; then
  echo "Deferring ${#deferred_regen[@]} derived file(s) to post-LLM re-derivation: ${deferred_regen[*]}"
fi
if [[ ${#modify_delete[@]} -gt 0 ]]; then
  echo "Modify/delete conflict(s) '${modify_delete[*]}' — each needs an explicit keep-or-delete verdict from the resolver, announced on the PR."
fi
emit_outputs "${needs_llm}" true
