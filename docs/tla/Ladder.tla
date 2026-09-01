------------------------------- MODULE Ladder -------------------------------
(* GENERATED FILE — do not edit. tests/_ladder_fsm_tla.py prints this       *)
(* module from the transition table in tests/_ladder_fsm_model.py;          *)
(* regenerate with `uv run python -m tests._ladder_fsm_tla`.                *)
(***************************************************************************)
(* The credential ladder's retry policy: the credential rungs of           *)
(* lib_credential_ladder.py's table, walked in order.  `pos` is the rung    *)
(* about to run, or "DONE" once the walk stops -- the only terminal marker, *)
(* since every action strictly advances it.  Rung 1's zero-cost error       *)
(* always advances (the same-token free retry); every later boundary needs  *)
(* its OWN configured credential, and run-ladder.py drops an unconfigured   *)
(* rung entirely, so an error steps OVER a gap rather than stopping at it.  *)
(* Python twin: tests/_ladder_fsm_model.py, proved against the shipped      *)
(* policy by tests/test_ladder_equivalence.py.                              *)
(***************************************************************************)

VARIABLE s

AllStates == [
    pos: {"1", "2", "3", "4", "5", "6", "7", "8", "DONE"},
    o1: {"NOT_RUN", "OK", "OK_ZERO", "ERR_PAID", "ERR_REFUSED", "ERR_WALL", "ERR_ZERO", "ERR_WALL_ZERO"},
    o2: {"NOT_RUN", "OK", "OK_ZERO", "ERR_PAID", "ERR_REFUSED", "ERR_WALL", "ERR_ZERO", "ERR_WALL_ZERO"},
    o3: {"NOT_RUN", "OK", "OK_ZERO", "ERR_PAID", "ERR_REFUSED", "ERR_WALL", "ERR_ZERO", "ERR_WALL_ZERO"},
    o4: {"NOT_RUN", "OK", "OK_ZERO", "ERR_PAID", "ERR_REFUSED", "ERR_WALL", "ERR_ZERO", "ERR_WALL_ZERO"},
    o5: {"NOT_RUN", "OK", "OK_ZERO", "ERR_PAID", "ERR_REFUSED", "ERR_WALL", "ERR_ZERO", "ERR_WALL_ZERO"},
    o6: {"NOT_RUN", "OK", "OK_ZERO", "ERR_PAID", "ERR_REFUSED", "ERR_WALL", "ERR_ZERO", "ERR_WALL_ZERO"},
    o7: {"NOT_RUN", "OK", "OK_ZERO", "ERR_PAID", "ERR_REFUSED", "ERR_WALL", "ERR_ZERO", "ERR_WALL_ZERO"},
    o8: {"NOT_RUN", "OK", "OK_ZERO", "ERR_PAID", "ERR_REFUSED", "ERR_WALL", "ERR_ZERO", "ERR_WALL_ZERO"},
    winner: {"NONE", "1", "2", "3", "4", "5", "6", "7", "8"},
    configured2: BOOLEAN,
    configured3: BOOLEAN,
    configured4: BOOLEAN,
    configured5: BOOLEAN,
    configured6: BOOLEAN,
    configured7: BOOLEAN,
    configured8: BOOLEAN
]

\* Nothing run, over every CONFIGURED combination.
Inits == {
    [pos |-> "1",
     o1 |-> "NOT_RUN", o2 |-> "NOT_RUN", o3 |-> "NOT_RUN", o4 |-> "NOT_RUN",
     o5 |-> "NOT_RUN", o6 |-> "NOT_RUN", o7 |-> "NOT_RUN", o8 |-> "NOT_RUN",
     winner |-> "NONE",
     configured2 |-> c2, configured3 |-> c3, configured4 |-> c4,
     configured5 |-> c5, configured6 |-> c6, configured7 |-> c7,
     configured8 |-> c8] :
    c2 \in BOOLEAN, c3 \in BOOLEAN, c4 \in BOOLEAN, c5 \in BOOLEAN,
    c6 \in BOOLEAN, c7 \in BOOLEAN, c8 \in BOOLEAN
}

Init == s \in Inits

Run1Ok ==
    /\ s.pos = "1"
    /\ s' = [s EXCEPT !.o1 = "OK", !.winner = "1", !.pos = "DONE"]

Run1OkZero ==
    /\ s.pos = "1"
    /\ s' = [s EXCEPT !.o1 = "OK_ZERO", !.winner = "1", !.pos = "DONE"]

Run1ErrPaid ==
    /\ s.pos = "1"
    /\ s' = [s EXCEPT !.o1 = "ERR_PAID", !.pos = IF s.configured2 THEN "2" ELSE "DONE"]

Run1ErrRefused ==
    /\ s.pos = "1"
    /\ s' = [s EXCEPT !.o1 = "ERR_REFUSED", !.pos = "DONE"]

Run1ErrWall ==
    /\ s.pos = "1"
    /\ s' = [s EXCEPT !.o1 = "ERR_WALL", !.pos = "DONE"]

Run1ErrZero ==
    /\ s.pos = "1"
    /\ s' = [s EXCEPT !.o1 = "ERR_ZERO", !.pos = "2"]

Run1ErrWallZero ==
    /\ s.pos = "1"
    /\ s' = [s EXCEPT !.o1 = "ERR_WALL_ZERO", !.pos = "DONE"]

Run2Ok ==
    /\ s.pos = "2"
    /\ s' = [s EXCEPT !.o2 = "OK", !.winner = "2", !.pos = "DONE"]

Run2OkZero ==
    /\ s.pos = "2"
    /\ s' = [s EXCEPT !.o2 = "OK_ZERO", !.winner = "2", !.pos = "DONE"]

Run2ErrPaid ==
    /\ s.pos = "2"
    /\ s' = [s EXCEPT !.o2 = "ERR_PAID", !.pos = IF s.configured3 THEN "3" ELSE IF s.configured4 THEN "4" ELSE IF s.configured5 THEN "5" ELSE IF s.configured6 THEN "6" ELSE IF s.configured7 THEN "7" ELSE IF s.configured8 THEN "8" ELSE "DONE"]

Run2ErrRefused ==
    /\ s.pos = "2"
    /\ s' = [s EXCEPT !.o2 = "ERR_REFUSED", !.pos = "DONE"]

Run2ErrWall ==
    /\ s.pos = "2"
    /\ s' = [s EXCEPT !.o2 = "ERR_WALL", !.pos = "DONE"]

Run2ErrZero ==
    /\ s.pos = "2"
    /\ s' = [s EXCEPT !.o2 = "ERR_ZERO", !.pos = IF s.configured3 THEN "3" ELSE IF s.configured4 THEN "4" ELSE IF s.configured5 THEN "5" ELSE IF s.configured6 THEN "6" ELSE IF s.configured7 THEN "7" ELSE IF s.configured8 THEN "8" ELSE "DONE"]

Run2ErrWallZero ==
    /\ s.pos = "2"
    /\ s' = [s EXCEPT !.o2 = "ERR_WALL_ZERO", !.pos = "DONE"]

Run3Ok ==
    /\ s.pos = "3"
    /\ s' = [s EXCEPT !.o3 = "OK", !.winner = "3", !.pos = "DONE"]

Run3OkZero ==
    /\ s.pos = "3"
    /\ s' = [s EXCEPT !.o3 = "OK_ZERO", !.winner = "3", !.pos = "DONE"]

Run3ErrPaid ==
    /\ s.pos = "3"
    /\ s' = [s EXCEPT !.o3 = "ERR_PAID", !.pos = IF s.configured4 THEN "4" ELSE IF s.configured5 THEN "5" ELSE IF s.configured6 THEN "6" ELSE IF s.configured7 THEN "7" ELSE IF s.configured8 THEN "8" ELSE "DONE"]

Run3ErrRefused ==
    /\ s.pos = "3"
    /\ s' = [s EXCEPT !.o3 = "ERR_REFUSED", !.pos = "DONE"]

Run3ErrWall ==
    /\ s.pos = "3"
    /\ s' = [s EXCEPT !.o3 = "ERR_WALL", !.pos = "DONE"]

Run3ErrZero ==
    /\ s.pos = "3"
    /\ s' = [s EXCEPT !.o3 = "ERR_ZERO", !.pos = IF s.configured4 THEN "4" ELSE IF s.configured5 THEN "5" ELSE IF s.configured6 THEN "6" ELSE IF s.configured7 THEN "7" ELSE IF s.configured8 THEN "8" ELSE "DONE"]

Run3ErrWallZero ==
    /\ s.pos = "3"
    /\ s' = [s EXCEPT !.o3 = "ERR_WALL_ZERO", !.pos = "DONE"]

Run4Ok ==
    /\ s.pos = "4"
    /\ s' = [s EXCEPT !.o4 = "OK", !.winner = "4", !.pos = "DONE"]

Run4OkZero ==
    /\ s.pos = "4"
    /\ s' = [s EXCEPT !.o4 = "OK_ZERO", !.winner = "4", !.pos = "DONE"]

Run4ErrPaid ==
    /\ s.pos = "4"
    /\ s' = [s EXCEPT !.o4 = "ERR_PAID", !.pos = IF s.configured5 THEN "5" ELSE IF s.configured6 THEN "6" ELSE IF s.configured7 THEN "7" ELSE IF s.configured8 THEN "8" ELSE "DONE"]

Run4ErrRefused ==
    /\ s.pos = "4"
    /\ s' = [s EXCEPT !.o4 = "ERR_REFUSED", !.pos = "DONE"]

Run4ErrWall ==
    /\ s.pos = "4"
    /\ s' = [s EXCEPT !.o4 = "ERR_WALL", !.pos = "DONE"]

Run4ErrZero ==
    /\ s.pos = "4"
    /\ s' = [s EXCEPT !.o4 = "ERR_ZERO", !.pos = IF s.configured5 THEN "5" ELSE IF s.configured6 THEN "6" ELSE IF s.configured7 THEN "7" ELSE IF s.configured8 THEN "8" ELSE "DONE"]

Run4ErrWallZero ==
    /\ s.pos = "4"
    /\ s' = [s EXCEPT !.o4 = "ERR_WALL_ZERO", !.pos = "DONE"]

Run5Ok ==
    /\ s.pos = "5"
    /\ s' = [s EXCEPT !.o5 = "OK", !.winner = "5", !.pos = "DONE"]

Run5OkZero ==
    /\ s.pos = "5"
    /\ s' = [s EXCEPT !.o5 = "OK_ZERO", !.winner = "5", !.pos = "DONE"]

Run5ErrPaid ==
    /\ s.pos = "5"
    /\ s' = [s EXCEPT !.o5 = "ERR_PAID", !.pos = IF s.configured6 THEN "6" ELSE IF s.configured7 THEN "7" ELSE IF s.configured8 THEN "8" ELSE "DONE"]

Run5ErrRefused ==
    /\ s.pos = "5"
    /\ s' = [s EXCEPT !.o5 = "ERR_REFUSED", !.pos = "DONE"]

Run5ErrWall ==
    /\ s.pos = "5"
    /\ s' = [s EXCEPT !.o5 = "ERR_WALL", !.pos = "DONE"]

Run5ErrZero ==
    /\ s.pos = "5"
    /\ s' = [s EXCEPT !.o5 = "ERR_ZERO", !.pos = IF s.configured6 THEN "6" ELSE IF s.configured7 THEN "7" ELSE IF s.configured8 THEN "8" ELSE "DONE"]

Run5ErrWallZero ==
    /\ s.pos = "5"
    /\ s' = [s EXCEPT !.o5 = "ERR_WALL_ZERO", !.pos = "DONE"]

Run6Ok ==
    /\ s.pos = "6"
    /\ s' = [s EXCEPT !.o6 = "OK", !.winner = "6", !.pos = "DONE"]

Run6OkZero ==
    /\ s.pos = "6"
    /\ s' = [s EXCEPT !.o6 = "OK_ZERO", !.winner = "6", !.pos = "DONE"]

Run6ErrPaid ==
    /\ s.pos = "6"
    /\ s' = [s EXCEPT !.o6 = "ERR_PAID", !.pos = IF s.configured7 THEN "7" ELSE IF s.configured8 THEN "8" ELSE "DONE"]

Run6ErrRefused ==
    /\ s.pos = "6"
    /\ s' = [s EXCEPT !.o6 = "ERR_REFUSED", !.pos = "DONE"]

Run6ErrWall ==
    /\ s.pos = "6"
    /\ s' = [s EXCEPT !.o6 = "ERR_WALL", !.pos = "DONE"]

Run6ErrZero ==
    /\ s.pos = "6"
    /\ s' = [s EXCEPT !.o6 = "ERR_ZERO", !.pos = IF s.configured7 THEN "7" ELSE IF s.configured8 THEN "8" ELSE "DONE"]

Run6ErrWallZero ==
    /\ s.pos = "6"
    /\ s' = [s EXCEPT !.o6 = "ERR_WALL_ZERO", !.pos = "DONE"]

Run7Ok ==
    /\ s.pos = "7"
    /\ s' = [s EXCEPT !.o7 = "OK", !.winner = "7", !.pos = "DONE"]

Run7OkZero ==
    /\ s.pos = "7"
    /\ s' = [s EXCEPT !.o7 = "OK_ZERO", !.winner = "7", !.pos = "DONE"]

Run7ErrPaid ==
    /\ s.pos = "7"
    /\ s' = [s EXCEPT !.o7 = "ERR_PAID", !.pos = IF s.configured8 THEN "8" ELSE "DONE"]

Run7ErrRefused ==
    /\ s.pos = "7"
    /\ s' = [s EXCEPT !.o7 = "ERR_REFUSED", !.pos = "DONE"]

Run7ErrWall ==
    /\ s.pos = "7"
    /\ s' = [s EXCEPT !.o7 = "ERR_WALL", !.pos = "DONE"]

Run7ErrZero ==
    /\ s.pos = "7"
    /\ s' = [s EXCEPT !.o7 = "ERR_ZERO", !.pos = IF s.configured8 THEN "8" ELSE "DONE"]

Run7ErrWallZero ==
    /\ s.pos = "7"
    /\ s' = [s EXCEPT !.o7 = "ERR_WALL_ZERO", !.pos = "DONE"]

Run8Ok ==
    /\ s.pos = "8"
    /\ s' = [s EXCEPT !.o8 = "OK", !.winner = "8", !.pos = "DONE"]

Run8OkZero ==
    /\ s.pos = "8"
    /\ s' = [s EXCEPT !.o8 = "OK_ZERO", !.winner = "8", !.pos = "DONE"]

Run8ErrPaid ==
    /\ s.pos = "8"
    /\ s' = [s EXCEPT !.o8 = "ERR_PAID", !.pos = "DONE"]

Run8ErrRefused ==
    /\ s.pos = "8"
    /\ s' = [s EXCEPT !.o8 = "ERR_REFUSED", !.pos = "DONE"]

Run8ErrWall ==
    /\ s.pos = "8"
    /\ s' = [s EXCEPT !.o8 = "ERR_WALL", !.pos = "DONE"]

Run8ErrZero ==
    /\ s.pos = "8"
    /\ s' = [s EXCEPT !.o8 = "ERR_ZERO", !.pos = "DONE"]

Run8ErrWallZero ==
    /\ s.pos = "8"
    /\ s' = [s EXCEPT !.o8 = "ERR_WALL_ZERO", !.pos = "DONE"]

Next ==
    \/ Run1Ok
    \/ Run1OkZero
    \/ Run1ErrPaid
    \/ Run1ErrRefused
    \/ Run1ErrWall
    \/ Run1ErrZero
    \/ Run1ErrWallZero
    \/ Run2Ok
    \/ Run2OkZero
    \/ Run2ErrPaid
    \/ Run2ErrRefused
    \/ Run2ErrWall
    \/ Run2ErrZero
    \/ Run2ErrWallZero
    \/ Run3Ok
    \/ Run3OkZero
    \/ Run3ErrPaid
    \/ Run3ErrRefused
    \/ Run3ErrWall
    \/ Run3ErrZero
    \/ Run3ErrWallZero
    \/ Run4Ok
    \/ Run4OkZero
    \/ Run4ErrPaid
    \/ Run4ErrRefused
    \/ Run4ErrWall
    \/ Run4ErrZero
    \/ Run4ErrWallZero
    \/ Run5Ok
    \/ Run5OkZero
    \/ Run5ErrPaid
    \/ Run5ErrRefused
    \/ Run5ErrWall
    \/ Run5ErrZero
    \/ Run5ErrWallZero
    \/ Run6Ok
    \/ Run6OkZero
    \/ Run6ErrPaid
    \/ Run6ErrRefused
    \/ Run6ErrWall
    \/ Run6ErrZero
    \/ Run6ErrWallZero
    \/ Run7Ok
    \/ Run7OkZero
    \/ Run7ErrPaid
    \/ Run7ErrRefused
    \/ Run7ErrWall
    \/ Run7ErrZero
    \/ Run7ErrWallZero
    \/ Run8Ok
    \/ Run8OkZero
    \/ Run8ErrPaid
    \/ Run8ErrRefused
    \/ Run8ErrWall
    \/ Run8ErrZero
    \/ Run8ErrWallZero

\* Every field stays within its declared domain -- a structural check on the
\* generated updates, not a restatement of anything Python already proves.
TypeOK == s \in AllStates

\* `evaluate`'s release rule: at least one rung ran, and every rung that ran
\* BILLED NOTHING.  The rule reads zero_cost alone and never errored, so a
\* zero-billed success and a zero-billed wall-clock failure both count here.
AllZeroCost ==
    /\ s.o1 \in {"NOT_RUN", "ERR_WALL_ZERO", "ERR_ZERO", "OK_ZERO"}
    /\ s.o2 \in {"NOT_RUN", "ERR_WALL_ZERO", "ERR_ZERO", "OK_ZERO"}
    /\ s.o3 \in {"NOT_RUN", "ERR_WALL_ZERO", "ERR_ZERO", "OK_ZERO"}
    /\ s.o4 \in {"NOT_RUN", "ERR_WALL_ZERO", "ERR_ZERO", "OK_ZERO"}
    /\ s.o5 \in {"NOT_RUN", "ERR_WALL_ZERO", "ERR_ZERO", "OK_ZERO"}
    /\ s.o6 \in {"NOT_RUN", "ERR_WALL_ZERO", "ERR_ZERO", "OK_ZERO"}
    /\ s.o7 \in {"NOT_RUN", "ERR_WALL_ZERO", "ERR_ZERO", "OK_ZERO"}
    /\ s.o8 \in {"NOT_RUN", "ERR_WALL_ZERO", "ERR_ZERO", "OK_ZERO"}

AnyRan ==
    \/ s.o1 # "NOT_RUN"
    \/ s.o2 # "NOT_RUN"
    \/ s.o3 # "NOT_RUN"
    \/ s.o4 # "NOT_RUN"
    \/ s.o5 # "NOT_RUN"
    \/ s.o6 # "NOT_RUN"
    \/ s.o7 # "NOT_RUN"
    \/ s.o8 # "NOT_RUN"

Released == AnyRan /\ AllZeroCost

\* The walk never continues past a winner.
WinnerImpliesDone == s.winner # "NONE" => s.pos = "DONE"

\* No rung past the free-retry boundary runs without its OWN credential.
RungRanRequiresConfigured ==
    /\ (s.o3 # "NOT_RUN" => s.configured3)
    /\ (s.o4 # "NOT_RUN" => s.configured4)
    /\ (s.o5 # "NOT_RUN" => s.configured5)
    /\ (s.o6 # "NOT_RUN" => s.configured6)
    /\ (s.o7 # "NOT_RUN" => s.configured7)
    /\ (s.o8 # "NOT_RUN" => s.configured8)

\* Rung 2 without its own credential is reached only via the free retry, and
\* only from a zero-cost error: a wall-clock failure never advances.
Rung2NeedsConfiguredOrFreeRetry ==
    (s.o2 # "NOT_RUN" /\ ~s.configured2) => s.o1 = "ERR_ZERO"

\* A wall-clock-only failure never advances, at ANY rung -- unlike ERR_PAID,
\* which steps on whenever a later rung holds its own credential.  A fresh
\* credential faces the identical wall.
NoAdvanceFromWall ==
    /\ (s.o1 \in {"ERR_WALL", "ERR_WALL_ZERO"} => s.pos = "DONE")
    /\ (s.o2 \in {"ERR_WALL", "ERR_WALL_ZERO"} => s.pos = "DONE")
    /\ (s.o3 \in {"ERR_WALL", "ERR_WALL_ZERO"} => s.pos = "DONE")
    /\ (s.o4 \in {"ERR_WALL", "ERR_WALL_ZERO"} => s.pos = "DONE")
    /\ (s.o5 \in {"ERR_WALL", "ERR_WALL_ZERO"} => s.pos = "DONE")
    /\ (s.o6 \in {"ERR_WALL", "ERR_WALL_ZERO"} => s.pos = "DONE")
    /\ (s.o7 \in {"ERR_WALL", "ERR_WALL_ZERO"} => s.pos = "DONE")
    /\ (s.o8 \in {"ERR_WALL", "ERR_WALL_ZERO"} => s.pos = "DONE")

Inv ==
    /\ TypeOK
    /\ WinnerImpliesDone
    /\ RungRanRequiresConfigured
    /\ Rung2NeedsConfiguredOrFreeRetry
    /\ NoAdvanceFromWall

\* No release invariant sits in Inv: `Released` is DEFINED from the outcomes, so
\* every predicate written over it here is true of any model and proves nothing.
\* What the release rule is held to is the equivalence proof against `evaluate`
\* in tests/test_ladder_equivalence.py.  The two witnesses below are what this
\* module can say about it -- both of them surprising, and both real.

\* At most one attempt per rung: once an outcome is recorded, no later step
\* changes it -- the walk never revisits a rung it already ran.
AtMostOneAttempt ==
    /\ [][ s.o1 # "NOT_RUN" => s'.o1 = s.o1 ]_s
    /\ [][ s.o2 # "NOT_RUN" => s'.o2 = s.o2 ]_s
    /\ [][ s.o3 # "NOT_RUN" => s'.o3 = s.o3 ]_s
    /\ [][ s.o4 # "NOT_RUN" => s'.o4 = s.o4 ]_s
    /\ [][ s.o5 # "NOT_RUN" => s'.o5 = s.o5 ]_s
    /\ [][ s.o6 # "NOT_RUN" => s'.o6 = s.o6 ]_s
    /\ [][ s.o7 # "NOT_RUN" => s'.o7 = s.o7 ]_s
    /\ [][ s.o8 # "NOT_RUN" => s'.o8 = s.o8 ]_s

\* Winner uniqueness: once chosen, the winner never changes.
WinnerStable == [][ s.winner # "NONE" => s'.winner = s.winner ]_s

\* Non-vacuity witnesses (EXPECT-EXIT 12 in the .cfg): TLC's counterexample
\* trace over the negation IS the reachability proof.
NoFreeRetry == ~(s.o1 = "ERR_ZERO" /\ ~s.configured2 /\ s.o2 # "NOT_RUN")

NoFullPaidWalk ==
    ~(/\ s.o1 = "ERR_PAID" /\ s.o2 = "ERR_PAID" /\ s.o3 = "ERR_PAID"
      /\ s.o4 = "ERR_PAID" /\ s.o5 = "ERR_PAID" /\ s.o6 = "ERR_PAID"
      /\ s.o7 = "ERR_PAID" /\ s.o8 = "ERR_PAID"
      /\ s.pos = "DONE")

\* A wall-clock-only failure at rung 3 ends the walk even though rung 4 has
\* its OWN distinct credential configured -- the stop is the wall's doing, not
\* an absent credential's, which is what tells it apart from ERR_PAID.
NoWallDespiteConfigured ==
    ~(s.o3 = "ERR_WALL" /\ s.configured4 /\ s.pos = "DONE")

\* run-ladder.py's _slots() drops an unconfigured rung before the walk, so an
\* error at rung 2 steps OVER the gap to the next rung that HAS its own
\* credential.  A model that stopped at the gap would describe a ladder that
\* never reaches the credentials behind it.
NoSkipOverGap ==
    ~(s.o2 # "NOT_RUN" /\ ~s.configured3 /\ s.o4 # "NOT_RUN")

\* A zero-billed success both names a winner and hands the attempt mark back.
NoReleasedWinner == ~(Released /\ s.winner # "NONE")

\* A wall-clock-only failure that billed nothing hands the mark back too:
\* claude-run-errored.sh computes zero_cost and wall_clock_only from separate
\* tests, so a shard that died at the wall having reached no inference sets both.
NoReleasedWall == ~(Released /\ s.o1 = "ERR_WALL_ZERO")

=============================================================================
