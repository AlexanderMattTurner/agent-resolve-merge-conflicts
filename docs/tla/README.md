# TLA+ specifications

`Ladder.tla` models the auto-resolve credential ladder's retry policy: the credential slots of `lib_credential_ladder.py`'s table, walked in order, and the attempt-mark release at the end of the walk. The rung count and the outcome symbols are both DERIVED — the rungs from that table, the symbols from the three flags `claude-run-errored.sh` emits — so neither can drift out of the model. The policy it models ships in [`.github/resolver/auto-resolve/_ladder.py`](../../.github/resolver/auto-resolve/_ladder.py), and [`.github/resolver/auto-resolve/run-ladder.py`](../../.github/resolver/auto-resolve/run-ladder.py) is what walks it.

## What holds the three copies together

The rules exist in three places, and each pair is checked:

| Pair                                    | What checks it                                                                                  |
| --------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `_ladder.evaluate` and the Python model | `tests/test_ladder_equivalence.py` — exhaustive over the model's whole reachable set            |
| The Python model and `Ladder.tla`       | `tests/test_ladder_fsm_tla.py` — the committed module must equal the emitter's output right now |
| `Ladder.tla` and its theorems           | `.github/scripts/checks/tla-model-check.py` — TLC runs every `.cfg` beside the module           |

`Ladder.tla` is GENERATED from the transition table in `tests/_ladder_fsm_model.py`. Edit the table, then run:

```bash
uv run python -m tests._ladder_fsm_tla
```

## Reading a config

Each `.cfg` declares the verdict TLC must reach, in the file the run already reads:

- `\* EXPECT-EXIT: 0` — the theorem holds. TLC exits 0 clean, 12 on a violated `INVARIANT`, 13 on a violated `PROPERTY`.
- `\* EXPECT-EXIT: 12` — an existence theorem. The invariant is stated as a negation, so TLC's counterexample trace IS the proof that the state is reachable. A clean pass is this config's failure.
- `\* EXPECT-DISTINCT: <n>` — the size of the set the run explored. Only a config that explores its whole state space carries one, so a model edit that silently moves the reachable set reds instead of passing.

A config with no `EXPECT-EXIT` line is a hard error: a default would let a new theorem join the suite unjudged.

| Config                  | Claim                                                                                                                  |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `Ladder_safety`         | The safety scheme: winner-once, configured-only rungs, no advance out of a wall-clock-only failure                     |
| `Ladder_winner`         | The winner never changes once set                                                                                      |
| `Ladder_freeretry`      | Witness — the free same-credential retry on a zero-cost error is reachable                                             |
| `Ladder_paidwall`       | Witness — every rung configured and paid, and the walk keeps its attempt mark                                          |
| `Ladder_wallclock`      | Witness — a wall-clock-only failure ends the walk despite the next rung's own credential                               |
| `Ladder_skipgap`        | Witness — an error steps OVER an unconfigured rung to the credential behind it, because `_slots()` drops the unset one |
| `Ladder_releasedwinner` | Witness — a zero-billed success both names a winner and releases the attempt mark                                      |
| `Ladder_releasedwall`   | Witness — a zero-billed wall-clock failure releases the mark too                                                       |

No release INVARIANT sits in `Ladder_safety`. `Released` is defined from the recorded outcomes, so any predicate written over it inside the module is true of every model and proves nothing. What holds the release rule to the shipped policy is `tests/test_ladder_equivalence.py`. The last two witnesses are what the module itself can say about it, and a reader would predict neither.

## Running TLC

```bash
python3 .github/scripts/checks/tla-model-check.py            # every config
python3 .github/scripts/checks/tla-model-check.py --only Ladder_safety
```

The pinned `tla2tools.jar` is downloaded and sha256-verified by `.github/scripts/install-tla2tools.sh`, from the version and digest in `.github/tool-versions.sh`. The run needs a JRE on `PATH`; a missing `java` is a hard error, never a skip, because a run that could not check the models has verified nothing.
