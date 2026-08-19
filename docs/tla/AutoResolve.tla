----------------------------- MODULE AutoResolve -----------------------------
(* GENERATED FILE — do not edit. tests/_outcome_fsm_tla.py prints this      *)
(* module from the transition table in tests/_outcome_fsm_model.py;         *)
(* regenerate with `uv run python -m tests._outcome_fsm_tla`.               *)
(***************************************************************************)
(* ONE auto-resolve run, from the pull request it was dispatched for to the *)
(* verdict its outcome gate reports.  `phase` walks SELECT, CLAIM, RESOLVE, *)
(* LAND and then DONE -- the only terminal marker, since every action       *)
(* strictly advances it.  A run can stop at any phase, so each phase has a  *)
(* transition that ends the run and writes its verdict.                     *)
(* Python twin: tests/_outcome_fsm_model.py, proved against the shipped     *)
(* rule in .github/resolver/auto-resolve/outcome.py by                      *)
(* tests/test_outcome_equivalence.py.                                       *)
(***************************************************************************)

VARIABLE s

AllStates == [
    phase: {"SELECT", "CLAIM", "RESOLVE", "LAND", "DONE"},
    selected: BOOLEAN,
    claim: {"NONE", "OWNED", "DUPLICATE", "LATCHED"},
    published: {"NONE", "NO_OP", "HANDOFF", "DECLINE"},
    land: {"NOT_RUN", "PUSHED", "NO_BUNDLE", "SUPERSEDED", "NOT_NEEDED", "QUEUE_HELD", "FAILED"},
    verdict: {"NONE", "refused", "gave_up", "landed", "superseded", "already_clear", "held", "land_failed", "no_op", "handed_off", "duplicate", "latched"}
]

Init == s = [phase |-> "SELECT", selected |-> FALSE, claim |-> "NONE", published |-> "NONE", land |-> "NOT_RUN", verdict |-> "NONE"]

SelectNone ==
    /\ s.phase = "SELECT"
    /\ s' = [s EXCEPT !.selected = FALSE, !.phase = "DONE", !.verdict = "refused"]

SelectPr ==
    /\ s.phase = "SELECT"
    /\ s' = [s EXCEPT !.selected = TRUE, !.phase = "CLAIM"]

ClaimNone ==
    /\ s.phase = "CLAIM"
    /\ s' = [s EXCEPT !.claim = "NONE", !.phase = "DONE", !.verdict = "gave_up"]

ClaimOwned ==
    /\ s.phase = "CLAIM"
    /\ s' = [s EXCEPT !.claim = "OWNED", !.phase = "RESOLVE"]

ClaimDuplicate ==
    /\ s.phase = "CLAIM"
    /\ s' = [s EXCEPT !.claim = "DUPLICATE", !.phase = "DONE", !.verdict = "duplicate"]

ClaimLatched ==
    /\ s.phase = "CLAIM"
    /\ s' = [s EXCEPT !.claim = "LATCHED", !.phase = "DONE", !.verdict = "latched"]

PublishNone ==
    /\ s.phase = "RESOLVE"
    /\ s' = [s EXCEPT !.published = "NONE", !.phase = "LAND"]

PublishNoOp ==
    /\ s.phase = "RESOLVE"
    /\ s' = [s EXCEPT !.published = "NO_OP", !.phase = "LAND"]

PublishHandoff ==
    /\ s.phase = "RESOLVE"
    /\ s' = [s EXCEPT !.published = "HANDOFF", !.phase = "LAND"]

PublishDecline ==
    /\ s.phase = "RESOLVE"
    /\ s' = [s EXCEPT !.published = "DECLINE", !.phase = "LAND"]

LandNotRun ==
    /\ s.phase = "LAND"
    /\ s' = [s EXCEPT !.land = "NOT_RUN", !.phase = "DONE", !.verdict = IF s.published = "NONE" THEN "gave_up" ELSE IF s.published = "NO_OP" THEN "no_op" ELSE IF s.published = "HANDOFF" THEN "handed_off" ELSE "handed_off"]

LandPushed ==
    /\ s.phase = "LAND"
    /\ s' = [s EXCEPT !.land = "PUSHED", !.phase = "DONE", !.verdict = "landed"]

LandNoBundle ==
    /\ s.phase = "LAND"
    /\ s' = [s EXCEPT !.land = "NO_BUNDLE", !.phase = "DONE", !.verdict = IF s.published = "NONE" THEN "gave_up" ELSE IF s.published = "NO_OP" THEN "no_op" ELSE IF s.published = "HANDOFF" THEN "handed_off" ELSE "handed_off"]

LandSuperseded ==
    /\ s.phase = "LAND"
    /\ s' = [s EXCEPT !.land = "SUPERSEDED", !.phase = "DONE", !.verdict = "superseded"]

LandNotNeeded ==
    /\ s.phase = "LAND"
    /\ s' = [s EXCEPT !.land = "NOT_NEEDED", !.phase = "DONE", !.verdict = "already_clear"]

LandQueueHeld ==
    /\ s.phase = "LAND"
    /\ s' = [s EXCEPT !.land = "QUEUE_HELD", !.phase = "DONE", !.verdict = "held"]

LandFailed ==
    /\ s.phase = "LAND"
    /\ s' = [s EXCEPT !.land = "FAILED", !.phase = "DONE", !.verdict = "land_failed"]

Next ==
    \/ SelectNone
    \/ SelectPr
    \/ ClaimNone
    \/ ClaimOwned
    \/ ClaimDuplicate
    \/ ClaimLatched
    \/ PublishNone
    \/ PublishNoOp
    \/ PublishHandoff
    \/ PublishDecline
    \/ LandNotRun
    \/ LandPushed
    \/ LandNoBundle
    \/ LandSuperseded
    \/ LandNotNeeded
    \/ LandQueueHeld
    \/ LandFailed

\* Every field stays within its declared domain -- a structural check on the
\* generated updates.
TypeOK == s \in AllStates

\* The verdicts that mean the conflict is still there and nothing else carries
\* it. outcome.py's `stall` flag decides the membership, so this set cannot
\* disagree with the exit status the gate reports.
Stall == s.verdict \in {"gave_up", "handed_off", "land_failed", "latched"}

\* Totality: a run that ended carries a verdict.  Without this an enum member
\* added with no arm would end a run classified as NONE, which the gate reads as
\* neither a stall nor a success.
TerminalHasVerdict == s.phase = "DONE" => s.verdict # "NONE"

\* THE CLAIM THIS MODULE EXISTS FOR -- a run that resolves nothing must not
\* report success.  Read the antecedent as the four facts that together mean the
\* conflict is still there and nobody else is on the hook for it: this run took
\* the pull request on, no other run holds the head, there was a merge to make,
\* and the land job neither pushed nor handed the head to a later run.  Every
\* such ending has to be a stall, which is what the gate exits non-zero on.
ConflictStandsImpliesStall ==
    (   /\ s.phase = "DONE"
        /\ s.selected
        /\ s.claim # "DUPLICATE"
        /\ s.published # "NO_OP"
        /\ s.land \notin ({"PUSHED", "NOT_NEEDED"} \union {"SUPERSEDED", "QUEUE_HELD"})
    ) => Stall

\* A landed resolution is never a stall.  The two sets are defined apart, so
\* this is what stops a future edit putting an ending in both.
LandedIsNotStall == s.verdict = "landed" => ~Stall

Inv ==
    /\ TypeOK
    /\ TerminalHasVerdict
    /\ ConflictStandsImpliesStall
    /\ LandedIsNotStall

\* A verdict is written once, by the transition that ends the run, and no later
\* step rewrites it.
VerdictStable == [][ s.verdict # "NONE" => s'.verdict = s.verdict ]_s

\* Non-vacuity witnesses (EXPECT-EXIT 12 in the .cfg): TLC's counterexample
\* trace over the negation IS the reachability proof.

\* A head latched by an attempt mark whose run cannot be identified is reachable,
\* and it is a stall.  This is the ending that used to report success: every step
\* after the stand-down skipped, and the run concluded green having landed
\* nothing.
NoLatchedStall == ~(s.verdict = "latched")

\* A paid run that asks a human to resolve the conflict is a stall too.  It
\* published a verdict, so the pull request says something -- but the conflict is
\* still there, and a green run reaches no failure route.
NoHandedOffStall == ~(s.verdict = "handed_off")

\* And the converse a reader would not predict from the claim above: a run can
\* end WITHOUT pushing and still report success, when the ending names who
\* carries the conflict instead -- another live run, a fresh run already
\* dispatched, or the merge queue.
NoGreenWithoutPush ==
    ~(s.phase = "DONE" /\ ~Stall /\ s.land # "PUSHED")

=============================================================================
