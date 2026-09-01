#!/usr/bin/env node
/**
 * PreToolUse hook for ONE auto-resolve shard: grant the file-write permission the
 * shard actually needs, and refuse every other write.
 *
 * The DENY is what this buys. `--permission-mode acceptEdits` lets a shard write any
 * path in the workspace, so "edit ONLY your file" was a prompt instruction that
 * `bundle.py`'s out-of-set guard could only catch after a concurrent shard had already
 * clobbered its sibling's file. This refusal is what keeps one shard out of another's
 * file, and what enforces `sidecar_prompt`'s "deliver to the scratch path".
 *
 * The allow is a belt, not the fix for the `.claude/` class — `lib.sh`'s sidecar
 * channel owns that, and it works by never needing the write. Claude Code asks a human
 * before writing some sensitive paths, a headless `-p` run has nobody to ask, and a
 * hook `allow` does NOT outrank that ask. Treat the allow as granting the ordinary
 * paths `acceptEdits` already granted, and route an unwritable class to the sidecar.
 *
 * The one file it grants may be a supervision file (`.claude/hooks/**`, a deny list)
 * when that is what conflicted; the compensating control is unchanged — prepare flags
 * a protected path, bundle.py says so, and the merge still faces CI and review.
 *
 * Failure posture. A hook that crashes is non-blocking to Claude Code, which falls
 * back to the same ask-then-deny that exists without this hook, so a broken hook
 * loses the grant rather than widening it.
 *
 * Env (both set by fanout.py):
 *   _AUTO_RESOLVE_SHARD_TARGET   newline-separated absolute path(s) this run
 *                                delivers. A resolve shard gets ONE — the
 *                                conflicted file itself, or the out-of-repo
 *                                scratch path when it took the sidecar prompt;
 *                                the hook-repair pass gets the whole resolved set.
 *   _AUTO_RESOLVE_SHARD_VERDICT  absolute path of its keep-or-delete verdict file,
 *                                empty for a shard with no modify/delete verdict
 *   _AUTO_RESOLVE_SHARD_DECLINE  absolute path of its decline record — the file it
 *                                states a refusal to merge in — empty for a shard
 *                                whose verdict file already carries `decline`
 *   _AUTO_RESOLVE_SHARD_WIDENED  newline-separated absolute paths the shard may
 *                                EDIT but not overwrite: the unconflicted files
 *                                this PR changed, where a conflict's correct
 *                                resolution sometimes lives (lib.sh writable_paths)
 */
import { resolve } from "node:path";

import { isMain } from "../lib/cli-args.mjs";

/** Tools that write a path; each carries it as `file_path`. */
const WRITE_TOOLS = new Set(["Edit", "Write", "MultiEdit", "NotebookEdit"]);

/**
 * The write tools a WIDENED path admits. Edit and MultiEdit need the file to
 * exist and its current text to match what the shard read, so a widened grant
 * can neither create a path nor overwrite one unread; Write can do both. Two
 * shards editing one widened file are serialized by that same check: the
 * later Edit fails on stale text and the shard reads again.
 */
const EDIT_ONLY_TOOLS = new Set(["Edit", "MultiEdit"]);

/** Tools that READ a path. Read names it `file_path`, Grep and Glob `path`. */
const READ_TOOLS = new Set(["Read", "Grep", "Glob"]);

/**
 * The verdict for one PreToolUse payload, or null to leave the call to Claude
 * Code's own permission flow (every non-writing tool).
 * @param {{tool_name: string, tool_input?: {file_path?: unknown}}} payload
 * @param {{targets: string[], verdict: string, decline: string, widened?: string[]}} grants
 * @returns {{permissionDecision: string, permissionDecisionReason: string} | null}
 */
export function judgeShardWrite(payload, grants) {
  if (!WRITE_TOOLS.has(payload?.tool_name)) return null;
  // ONE spelling of the grant set, and every refusal below names it. A second
  // spelling drifts the moment a fourth grant lands, and a refusal that names a
  // narrower set than the code allows tells a shard that declining is
  // impossible — which is the one thing it must always be able to do.
  const allowed = [...grants.targets, grants.verdict, grants.decline].filter(
    Boolean,
  );
  const widened = grants.widened ?? [];
  const named =
    allowed.join(", ") +
    (widened.length > 0
      ? `, and Edit (never Write) ${widened.join(", ")}`
      : "");
  const path = payload?.tool_input?.file_path;
  // A write tool whose path is unreadable is refused rather than passed through:
  // passing it through would hand the decision to the flow this hook exists to
  // override, and no legitimate shard write arrives without a file_path.
  if (typeof path !== "string" || path === "")
    return {
      permissionDecision: "deny",
      permissionDecisionReason: `${payload.tool_name} carried no file_path; this shard may write only ${named}.`,
    };
  if (allowed.includes(resolve(path)))
    return {
      permissionDecision: "allow",
      permissionDecisionReason: `${path} is this shard's assigned path.`,
    };
  if (widened.includes(resolve(path))) {
    if (EDIT_ONLY_TOOLS.has(payload.tool_name))
      return {
        permissionDecision: "allow",
        permissionDecisionReason: `${path} is a file this PR changed; the resolution may edit it.`,
      };
    return {
      permissionDecision: "deny",
      permissionDecisionReason: `${payload.tool_name} would replace ${path} whole. This PR changed that file, so the resolution may Edit lines in it but never overwrite it. This shard may write only ${named}.`,
    };
  }
  return {
    permissionDecision: "deny",
    permissionDecisionReason: `This shard may write only ${named}. ${path} belongs to another shard or is outside the resolution.`,
  };
}

/**
 * The verdict for a READ on a run whose merged tree the resolver may not trust —
 * a fork head — or null when nothing confines this run.
 *
 * INVARIANT — this refusal is what stops a conflicted file's own text from
 * spending the shard's Read tool on the runner's environment. The shard has no
 * shell, and its writes already reach one file, so a read confined to the
 * worktree leaves an injected instruction no path to a credential:
 * `/proc/self/environ`, `~/.claude`, and the fan-out's own logs all sit outside
 * it. A run with no confinement (a same-repo head) keeps the ordinary flow.
 *
 * @param {{tool_name: string, tool_input?: Record<string, unknown>}} payload
 * @param {{targets: string[], verdict: string, decline: string, confineTo: string}} grants
 * @returns {{permissionDecision: string, permissionDecisionReason: string} | null}
 */
export function judgeShardRead(payload, grants) {
  if (!grants.confineTo) return null;
  if (!READ_TOOLS.has(payload?.tool_name)) return null;
  const raw = payload?.tool_input?.file_path ?? payload?.tool_input?.path;
  // No path at all is the tool's own default, which is the working directory —
  // inside the tree by construction, so there is nothing to refuse.
  if (raw === undefined || raw === null || raw === "") return null;
  if (typeof raw !== "string")
    return {
      permissionDecision: "deny",
      permissionDecisionReason: `${payload.tool_name} carried an unreadable path; this run may read only under ${grants.confineTo}.`,
    };
  const path = resolve(raw);
  const allowed = [...grants.targets, grants.verdict, grants.decline].filter(
    Boolean,
  );
  if (allowed.includes(path)) return null;
  if (path === grants.confineTo || path.startsWith(`${grants.confineTo}/`))
    return null;
  return {
    permissionDecision: "deny",
    permissionDecisionReason: `${path} is outside ${grants.confineTo}. This merge comes from a fork, so the resolution reads only the merged tree.`,
  };
}

/**
 * @param {NodeJS.ProcessEnv} env
 * @returns {{targets: string[], verdict: string, decline: string, widened: string[], confineTo: string}}
 */
export function grantsFromEnv(env) {
  const target = env._AUTO_RESOLVE_SHARD_TARGET;
  if (!target) throw new Error("_AUTO_RESOLVE_SHARD_TARGET is unset");
  const targets = target
    .split("\n")
    .filter(Boolean)
    .map((entry) => resolve(entry));
  if (targets.length === 0)
    throw new Error("_AUTO_RESOLVE_SHARD_TARGET names no path");
  return {
    targets,
    verdict: env._AUTO_RESOLVE_SHARD_VERDICT
      ? resolve(env._AUTO_RESOLVE_SHARD_VERDICT)
      : "",
    // The channel a shard says "I will not merge this" through. Granted for the
    // same reason the verdict file is: a shard that cannot write its answer has
    // no answer, and the run then reads its silence as a resolver fault.
    decline: env._AUTO_RESOLVE_SHARD_DECLINE
      ? resolve(env._AUTO_RESOLVE_SHARD_DECLINE)
      : "",
    widened: (env._AUTO_RESOLVE_SHARD_WIDENED ?? "")
      .split("\n")
      .filter(Boolean)
      .map((entry) => resolve(entry)),
    // The merged tree, and only on a run whose head the resolver does not
    // trust. `cwd` is that tree: the shard resolves every relative path it is
    // given against it, because the fan-out launches the CLI there. Empty on a
    // same-repo head, which confines no read.
    confineTo:
      env.AUTO_RESOLVE_UNTRUSTED_HEAD === "true" ? resolve(process.cwd()) : "",
  };
}

/** Read stdin to a string. @returns {Promise<string>} */
async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

if (isMain(import.meta.url)) {
  const payload = JSON.parse(await readStdin());
  const grants = grantsFromEnv(process.env);
  const verdict =
    judgeShardWrite(payload, grants) ?? judgeShardRead(payload, grants);
  if (verdict !== null)
    process.stdout.write(
      JSON.stringify({
        hookSpecificOutput: { hookEventName: "PreToolUse", ...verdict },
      }),
    );
}
