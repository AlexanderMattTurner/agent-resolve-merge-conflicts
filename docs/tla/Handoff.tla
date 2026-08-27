------------------------------- MODULE Handoff -------------------------------
(* GENERATED FILE — do not edit. tests/_handoff_fsm_tla.py prints this      *)
(* module from the transition table in tests/_handoff_fsm_model.py;         *)
(* regenerate with `uv run python -m tests._handoff_fsm_tla`.               *)
(***************************************************************************)
(* ONE head across TWO auto-resolve runs.  `phase` walks RUN1, MARK, RETRY  *)
(* and then DONE -- the only terminal marker, since every action strictly   *)
(* advances it.  `cause` is how the first run ended, `marked` whether that  *)
(* wrote the handoff attempt mark, and `retry` what the second run did.     *)
(* The single-run module beside this one ends at a verdict; this one starts *)
(* there, because a wrongly marked head costs the NEXT run, not this one.   *)
(* Python twin: tests/_handoff_fsm_model.py, proved against the shipped     *)
(* rule in .github/resolver/auto-resolve/_refusal.py by                     *)
(* tests/test_handoff_equivalence.py.                                       *)
(***************************************************************************)

VARIABLE s

AllStates == [
    phase: {"RUN1", "MARK", "RETRY", "DONE"},
    cause: {"NONE", "LANDED", "MERGE", "PLUMBING", "SUPERSEDED"},
    marked: BOOLEAN,
    retry: {"NOT_RUN", "RESOLVES", "STOOD_DOWN"}
]

Init == s = [phase |-> "RUN1", cause |-> "NONE", marked |-> FALSE, retry |-> "NOT_RUN"]

EndLanded ==
    /\ s.phase = "RUN1"
    /\ s' = [s EXCEPT !.cause = "LANDED", !.phase = "MARK"]

EndMerge ==
    /\ s.phase = "RUN1"
    /\ s' = [s EXCEPT !.cause = "MERGE", !.phase = "MARK"]

EndPlumbing ==
    /\ s.phase = "RUN1"
    /\ s' = [s EXCEPT !.cause = "PLUMBING", !.phase = "MARK"]

EndSuperseded ==
    /\ s.phase = "RUN1"
    /\ s' = [s EXCEPT !.cause = "SUPERSEDED", !.phase = "MARK"]

WriteTheMark ==
    /\ s.phase = "MARK"
    /\ s' = [s EXCEPT !.phase = "RETRY", !.marked = IF s.cause = "LANDED" THEN FALSE ELSE IF s.cause = "MERGE" THEN TRUE ELSE IF s.cause = "PLUMBING" THEN FALSE ELSE FALSE]

RetryWithNoMark ==
    /\ s.phase = "RETRY"
    /\ s.marked = FALSE
    /\ s' = [s EXCEPT !.retry = "RESOLVES", !.phase = "DONE"]

RetryAfterAMark ==
    /\ s.phase = "RETRY"
    /\ s.marked = TRUE
    /\ s' = [s EXCEPT !.retry = "STOOD_DOWN", !.phase = "DONE"]

Next ==
    \/ EndLanded
    \/ EndMerge
    \/ EndPlumbing
    \/ EndSuperseded
    \/ WriteTheMark
    \/ RetryWithNoMark
    \/ RetryAfterAMark

\* Every field stays within its declared domain -- a structural check on the
\* generated updates.
TypeOK == s \in AllStates

\* The second run reads the MARK, never the cause.  A retry that stood down
\* without one would be a stand-down nothing recorded, so this pins the retry
\* transitions to the only fact `discover` can actually see.
StandDownRequiresAMark == s.retry = "STOOD_DOWN" => s.marked

\* THE CLAIM THIS MODULE EXISTS FOR -- a run that failed for a reason the TREE
\* did not cause never strands the head.  The mark exists to stop the resolver
\* paying an LLM again for an answer whose inputs did not change; a binary this
\* job never installed is fixed OUTSIDE the pull request, so a re-run against
\* the same head answers differently and the mark would only cost the TTL.
\*
\* The two sides come from different declarations and that is what makes this a
\* theorem: the causes below are the ones `TREE_CAUSED` does NOT list, while
\* `marked` comes from the mark rule.  A rule that grew to mark a plumbing fault
\* reds here, and every call site's own test still passes.
FaultNeverStrandsTheHead ==
    ~( (s.cause = "LANDED" \/ s.cause = "PLUMBING" \/ s.cause = "SUPERSEDED") /\ s.retry = "STOOD_DOWN" )

Inv ==
    /\ TypeOK
    /\ StandDownRequiresAMark
    /\ FaultNeverStrandsTheHead

\* The mark is written once, by the transition after the run ends, and no later
\* step takes it back.
MarkIsStable == [][ s.marked => s'.marked ]_s

\* Non-vacuity witness (EXPECT-EXIT 12 in the .cfg): TLC's counterexample trace
\* over the negation IS the reachability proof.

\* The converse the claim above does not give, and the reason it is not
\* satisfied by never marking anything: a run the MERGE beat does still latch the
\* next one, which is the whole purpose of the mark.
NoLatchingEndingAtAll ==
    ~(s.cause \in {"MERGE"} /\ s.retry = "STOOD_DOWN")

=============================================================================
