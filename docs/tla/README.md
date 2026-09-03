# TLA+ specifications

Every module here is generated from a Python transition table and checked by TLC in CI.

`AutoResolve.tla` models one auto-resolve run, from the pull request it was dispatched for to the verdict its outcome gate reports. Its load-bearing theorem is `ConflictStandsImpliesStall`: a run that took a conflict on, resolved nothing, and named nobody else as carrying it must be a STALL, which the gate exits non-zero on. The policy it models ships in [`.github/resolver/auto-resolve/outcome.py`](../../.github/resolver/auto-resolve/outcome.py), and every enum it reads — the claim, the published verdict, the land ending — is derived from that module, so a member added there widens the model rather than escaping it.

`Handoff.tla` models one HEAD across TWO runs: how the first run ended, whether that wrote the handoff attempt mark, and what the second run then does against the same head. Its load-bearing theorem is `FaultNeverStrandsTheHead`: a run that failed for a reason the tree did not cause never marks the head, so a re-run is not stood down for a binary this job never installed. The rule it models ships in [`.github/resolver/auto-resolve/_refusal.py`](../../.github/resolver/auto-resolve/_refusal.py).

`Ladder.tla` models the auto-resolve credential ladder's retry policy: the credential slots of `lib_credential_ladder.py`'s table, walked in order, and the attempt-mark release at the end of the walk. The rung count and the outcome symbols are both DERIVED — the rungs from that table, the symbols from the three flags `claude-run-errored.sh` emits — so neither can drift out of the model. The policy it models ships in [`.github/resolver/auto-resolve/_ladder.py`](../../.github/resolver/auto-resolve/_ladder.py), and [`.github/resolver/auto-resolve/run-ladder.py`](../../.github/resolver/auto-resolve/run-ladder.py) is what walks it.

`ConflictLedger.tla` models the conflict ledger's disposition rule: N conflicted paths, each holding exactly ONE disposition, and `claim` as the only action. Its load-bearing theorem is `TerminalClaimIsFinal`: once a pass has staged, refused or handed a path to the model, no later pass changes that entry, and a DEFERRED path is finished only by the pass its `to` names. The disposition values and the TO_MODEL prompts are both DERIVED from [`.github/resolver/auto-resolve/_conflict_set.py`](../../.github/resolver/auto-resolve/_conflict_set.py), which is also the policy it models. `DisjointPartitions` is the one theorem that holds by CONSTRUCTION: one disposition field cannot carry two values, which is the whole difference from the twenty parallel bash arrays this ledger replaced. `N` is a TLC constant, and the module ASSUMEs the count its table was generated for.

## What holds the copies together

Each rule exists as shipped code, as a Python model and as a TLA+ module, and each pair is checked:

| Pair                                      | What checks it                                                                                     |
| ----------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `_ladder.evaluate` and the Python model   | `tests/test_ladder_equivalence.py` — exhaustive over the model's whole reachable set               |
| The Python model and `Ladder.tla`         | `tests/test_ladder_fsm_tla.py` — the committed module must equal the emitter's output right now    |
| Every module and its theorems             | `.github/scripts/checks/tla-model-check.py` — TLC runs every `.cfg` beside the module              |
| `outcome.verdict` and its Python model    | `tests/test_outcome_equivalence.py` — the whole enum product, plus the gate's real exit status     |
| The Python model and `AutoResolve.tla`    | `tests/test_outcome_fsm_tla.py` — the committed module must equal the emitter's output right now   |
| `_refusal.fail` and the Python model      | `tests/test_handoff_equivalence.py` — every ending the first run can report                        |
| The Python model and `Handoff.tla`        | `tests/test_handoff_fsm_tla.py` — the committed module must equal the emitter's output right now   |
| `ConflictSet.claim` and its Python model  | `tests/test_ledger_equivalence.py` — every entry the model reaches, by every pass, for every claim |
| The Python model and `ConflictLedger.tla` | `tests/test_ledger_fsm_tla.py` — the committed module must equal the emitter's output right now    |

Every module is GENERATED from a transition table under `tests/`, through the shared printer in `tests/_fsm_tla.py`. Edit the table, then run its emitter:

```bash
uv run python -m tests._ladder_fsm_tla
uv run python -m tests._outcome_fsm_tla
uv run python -m tests._handoff_fsm_tla
uv run python -m tests._ledger_fsm_tla
```

## Reading a config

Each `.cfg` declares the verdict TLC must reach, in the file the run already reads:

- `\* EXPECT-EXIT: 0` — the theorem holds. TLC exits 0 clean, 12 on a violated `INVARIANT`, 13 on a violated `PROPERTY`.
- `\* EXPECT-EXIT: 12` — an existence theorem. The invariant is stated as a negation, so TLC's counterexample trace IS the proof that the state is reachable. A clean pass is this config's failure.
- `\* EXPECT-DISTINCT: <n>` — the size of the set the run explored. Only a config that explores its whole state space carries one, so a model edit that silently moves the reachable set reds instead of passing.

A config with no `EXPECT-EXIT` line is a hard error: a default would let a new theorem join the suite unjudged.

| Config                         | Claim                                                                                                                         |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| `Ladder_safety`                | The safety scheme: winner-once, configured-only rungs, no advance out of a wall-clock-only failure                            |
| `Ladder_winner`                | The winner never changes once set                                                                                             |
| `Ladder_freeretry`             | Witness — the free same-credential retry on a zero-cost error is reachable                                                    |
| `Ladder_paidwall`              | Witness — every rung configured and paid, and the walk keeps its attempt mark                                                 |
| `Ladder_wallclock`             | Witness — a wall-clock-only failure ends the walk despite the next rung's own credential                                      |
| `Ladder_skipgap`               | Witness — an error steps OVER an unconfigured rung to the credential behind it, because `_slots()` drops the unset one        |
| `Ladder_releasedwinner`        | Witness — a zero-billed success both names a winner and releases the attempt mark                                             |
| `Ladder_releasedwall`          | Witness — a zero-billed wall-clock failure releases the mark too                                                              |
| `AutoResolve_safety`           | Every ended run carries a verdict, and every unresolved ending with nobody on the hook is a stall                             |
| `AutoResolve_latched`          | Witness — the stand-down on an unidentifiable attempt mark is reachable, and it reds the run                                  |
| `AutoResolve_handoff`          | Witness — a published handoff verdict is a stall, so a run that asks a human reaches the failure route                        |
| `AutoResolve_greenwithoutpush` | Witness — a run can push nothing and still report success, when its ending names who carries the conflict                     |
| `Handoff_safety`               | Only a merge the resolver could not do latches the next run                                                                   |
| `Handoff_latching`             | Witness — the latching ending is reachable, so the mark is not satisfied by never marking anything                            |
| `ConflictLedger_safety`        | One disposition per path, each argument field on the state that owns it, and no second claim on a terminal path               |
| `ConflictLedger_settled`       | A ledger whose every path is terminal takes no further step                                                                   |
| `ConflictLedger_handoff`       | Witness — a deferred path is claimed again by the pass its `to` names, and reaches STAGED                                     |
| `ConflictLedger_stuck`         | Witness — every pass has claimed something and a path is still UNCLAIMED, which is what `require_fully_dispositioned` refuses |

No release INVARIANT sits in `Ladder_safety`. `Released` is defined from the recorded outcomes, so any predicate written over it inside the module is true of every model and proves nothing. What holds the release rule to the shipped policy is `tests/test_ladder_equivalence.py`. The last two witnesses are what the module itself can say about it, and a reader would predict neither.

## Running TLC

```bash
python3 .github/scripts/checks/tla-model-check.py            # every config
python3 .github/scripts/checks/tla-model-check.py --only Ladder_safety
```

The pinned `tla2tools.jar` is downloaded and sha256-verified by `.github/scripts/install-tla2tools.sh`, from the version and digest in `.github/tool-versions.sh`. The run needs a JRE on `PATH`; a missing `java` is a hard error, never a skip, because a run that could not check the models has verified nothing.
