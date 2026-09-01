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
# LLM gives a keep-or-delete verdict); needs_llm/needs_commit; no_op_head (the
# attempt mark this run gives back, no-op exits only).
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

# no_op_exit REASON — end the run having changed nothing, LOUDLY. Discovery
# reported this PR conflicted, but prepare found nothing to resolve, so this
# hands back the attempt mark (`no_op_head`) for a later scan to retry, rather
# than suppress the PR until the mark's TTL.
no_op_exit() {
  echo "::warning::Auto-resolve made no change to PR #${PR_NUMBER:-?} (${HEAD_REF}): $1. Discovery reported this PR conflicted, so the two disagree — this run resolved nothing, so it releases ${pre_merge_head}'s attempt mark and a later scan may retry it."
  {
    echo "needs_llm=false"
    echo "needs_commit=false"
    echo "no_op_head=${pre_merge_head}"
  } >>"$out"
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
pre_pass="${AUTO_RESOLVE_PRE_PASS:-}"
post_merge_check="${AUTO_RESOLVE_POST_MERGE_CHECK:-}"

# Same shape as the mergiraf pre-flight below, and here each saves a whole billed
# resolution: `bundle.py` runs both of these AFTER the model has resolved every
# shard, and a binary the job never installs refuses there with nothing landed.
# Skipped for a fork head, the one run `bundle.py` empties both of its copies for.
# Called as a plain command, never in `$(…)`, so this `exit` reaches the caller.
refuse_a_caller_tool_the_runner_lacks() {
  local input="$1" cmd="$2" lost="$3" bin="${2%% *}"
  [[ -n "$cmd" && "${AUTO_RESOLVE_UNTRUSTED_HEAD:-}" != "true" ]] || return 0
  command -v "$bin" >/dev/null && return 0
  echo "auto-resolve/prepare: the \`${input}\` binary '${bin}' is not on this runner's PATH — install it in the calling workflow, or clear \`${input}\`; refusing before this run buys a resolution it could not ${lost}." >&2
  exit 78 # EXIT_MISCONFIGURED — the caller's wiring, not this tree's conflict.
}
refuse_a_caller_tool_the_runner_lacks pre-pass-command "$pre_pass" re-derive
refuse_a_caller_tool_the_runner_lacks post-merge-check-command "$post_merge_check" check

merge_rc=0
git merge --no-edit "$base_ref_name" || merge_rc=$?
install_merged_node_deps

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
  if [[ -n "$resolver_mjs" ]]; then
    owned_file="$(mktemp)"
    # Fails CLOSED for the reason the partition's oracle does: an unreadable
    # ownership answer would route a caller-owned lockfile to the built-in rules.
    node "$resolver_mjs" --owned >"$owned_file" || {
      echo "auto-resolve/prepare: 'node ${resolver_mjs} --owned' failed; refusing to route lockfiles without an ownership answer." >&2
      exit 1
    }
    route_args+=(--owned-file "$owned_file")
  fi
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
    # shellcheck disable=SC2086
    # echo-fallback-ok: a GitHub warning annotation, not a value. The pre-pass is
    # advisory here — bundle re-runs it and verifies the bytes byte-for-byte.
    $pre_pass || echo "::warning::the derived-file pre-pass exited non-zero re-deriving a cleanly-merged lockfile; the paths it owns keep the bytes git merged."
    git add -A
  fi
fi
if [[ ${#builtin_refused[@]} -gt 0 && "$merge_rc" -eq 0 ]]; then
  echo "Unresolvable lockfile(s) '${builtin_refused[*]}' — handing off rather than pushing bytes no lock command produces."
  {
    echo "needs_llm=false"
    echo "needs_commit=false"
    echo "unresolvable=${builtin_refused[*]}"
  } >>"$out"
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
  if [[ -n "$pre_pass" ]]; then
    # Word-split on purpose: the input is a command line, not one argument.
    # shellcheck disable=SC2086
    $pre_pass || prepass_rc=$?
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
    node "$resolver_mjs" --root="$PWD" || prepass_rc=$?
  fi
  if [[ "$prepass_rc" -ne 0 ]]; then
    echo "::warning::the derived-file pre-pass exited ${prepass_rc} (a generator crashed, the resolver would not load, or an output still carries markers); continuing — FINALIZE re-runs it and verifies generated content byte-for-byte."
  fi
fi

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
rm -f "$region_defer_file"

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
  {
    echo "needs_llm=false"
    echo "needs_commit=true"
  } >>"$out"
  exit 0
fi

# Rule-owned paths, asked of the TRUSTED-BASE resolver under `node` (`pnpm`
# parses package.json, which mid-merge can carry markers; `--owned` parses no
# manifest). Fail CLOSED: an oracle answering "nothing is owned" when broken
# misroutes exactly the paths it exists to route.
#
# A caller that declared no resolver has no rule table, so there is nothing to
# ask and nothing to fail closed on — the empty answer is the true one there,
# and gb_is_generated_owned below says "not owned" for every path.
# shellcheck source=.github/resolver/lib/generated-owned.bash
source "$(dirname "${BASH_SOURCE[0]}")/../lib/generated-owned.bash"
if [[ -n "$resolver_mjs" ]]; then
  gb_load_generated_owned "$resolver_mjs" --owned || {
    echo "auto-resolve/prepare: 'node ${resolver_mjs} --owned' failed." >&2
    echo "Without an ownership answer, a re-derivable lockfile reads as unmergeable and goes to a human." >&2
    echo "This step refuses to partition instead." >&2
    exit 1
  }
fi

# Partition. An owned conflict's source ALSO conflicted — bundle re-derives
# it after the LLM resolves the source. A binary conflict, or a `-merge` file
# owned by no rule, has no markers and only a human can resolve it. A
# modify/delete conflict also has no markers, but the LLM can reach a verdict
# under its own prompt in `modify_delete` — the marker-free file LOOKS resolved.
llm_list=()
deferred_regen=()
unresolvable=("${builtin_refused[@]}")
modify_delete=()
structural_candidates=()
# A recognized lockfile the routing pass could not finish never reaches mergiraf
# or the model: a hand or structural resolution of one is a guess at what the
# lock command would produce.
declare -A builtin_lockfile=()
for f in "${builtin_deferred[@]}" "${builtin_refused[@]}"; do builtin_lockfile["$f"]=1; done
for f in "${conflicts[@]}"; do
  if [[ -n "${builtin_lockfile["$f"]:-}" ]]; then
    continue
  elif gb_is_generated_owned "$f" || [[ -n "${region_deferred["$f"]:-}" ]]; then
    deferred_regen+=("$f")
  elif is_unmergeable "$f" "$base_ref_name"; then
    unresolvable+=("$f")
  else
    if is_modify_delete "$f"; then
      modify_delete+=("$f")
    else
      structural_candidates+=("$f")
    fi
    llm_list+=("$f")
  fi
done

# An unresolvable path ALONE aborts: nothing else needs attention, so the full stop costs
# nothing. Beside other work, each unresolvable path keeps HEAD_REF's own content instead,
# because a merge commit cannot be created with a path left unmerged. That drops the base's
# edit to that one file, and land re-derives the drop from the pushed blobs and flags it —
# this step's own claim about it must not be the only record.
if [[ ${#unresolvable[@]} -gt 0 ]]; then
  if [[ ${#llm_list[@]} -eq 0 && ${#deferred_regen[@]} -eq 0 ]]; then
    echo "Unmergeable conflict(s) '${unresolvable[*]}' — no textual resolution exists; handing off to a human."
    {
      echo "needs_llm=false"
      echo "needs_commit=false"
      echo "unresolvable=${unresolvable[*]}"
    } >>"$out"
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
  llm_list=("${modify_delete[@]}" "${still_conflicted[@]}")
fi

# Marker-damaged paths join the partition last, after mergiraf rewrites `llm_list`. A
# rule-owned one stays AWAY FROM THE LLM: hand-editing markers out of a derived file yields
# bytes BUNDLE's `--verify` refuses, and resolve-generated only re-derives paths git reports
# unmerged. The rest carry ordinary marker text and go to the ordinary marker prompt, not to
# mergiraf, whose `solve` expects markers git wrote.
for f in "${marker_damaged[@]}"; do
  if gb_is_generated_owned "$f"; then
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
{
  echo "needs_llm=${needs_llm}"
  echo "needs_commit=true"
  echo "conflict_list=${llm_list[*]:-}"
  echo "deferred_regen=${deferred_regen[*]:-}"
  echo "deferred_lockfiles=${builtin_deferred[*]:-}"
  echo "modify_delete=${modify_delete[*]:-}"
  echo "sidecar=${sidecar[*]:-}"
  echo "unresolvable=${unresolvable[*]:-}"
} >>"$out"
