--------------------------- MODULE ConflictLedger ---------------------------
(* GENERATED FILE — do not edit. tests/_ledger_fsm_tla.py prints this       *)
(* module from the transition table in tests/_ledger_fsm_model.py;          *)
(* regenerate with `uv run python -m tests._ledger_fsm_tla`.                *)
(***************************************************************************)
(* The conflict ledger's disposition rule: one entry per conflicted path,   *)
(* each holding exactly ONE disposition.  A pass claims a path, which is    *)
(* the only action.  STAGED, REFUSED and TO_MODEL are the last word; a      *)
(* DEFERRED path is claimed again by the one pass its `to` names, and by    *)
(* nobody else.  N is the number of paths this table was generated for.     *)
(* Python twin: tests/_ledger_fsm_model.py, proved against the shipped      *)
(* ledger by tests/test_ledger_equivalence.py.                              *)
(***************************************************************************)

\* Naturals for `..`: the theorems below range over the path indices 1..N.
EXTENDS Naturals

CONSTANT N

\* The table below carries one field set per path, so a config that set N to
\* anything else would read a partition over paths the record does not hold.
ASSUME N = 2

VARIABLE s

AllStates == [
    d1: {"unclaimed", "staged", "deferred", "refused", "to_model"},
    by1: {"", "mergiraf", "prepare", "bundle"},
    to1: {"", "mergiraf", "prepare", "bundle"},
    prompt1: {"", "marker", "modify_delete", "sidecar"},
    handed1: BOOLEAN,
    d2: {"unclaimed", "staged", "deferred", "refused", "to_model"},
    by2: {"", "mergiraf", "prepare", "bundle"},
    to2: {"", "mergiraf", "prepare", "bundle"},
    prompt2: {"", "marker", "modify_delete", "sidecar"},
    handed2: BOOLEAN,
    ran_mergiraf: BOOLEAN,
    ran_prepare: BOOLEAN,
    ran_bundle: BOOLEAN
]

\* Nothing claimed yet.
Init == s = [
    d1 |-> "unclaimed", by1 |-> "", to1 |-> "", prompt1 |-> "", handed1 |-> FALSE,
    d2 |-> "unclaimed", by2 |-> "", to2 |-> "", prompt2 |-> "", handed2 |-> FALSE,
    ran_mergiraf |-> FALSE, ran_prepare |-> FALSE, ran_bundle |-> FALSE]

Claim1MergirafStaged ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "staged", !.by1 = "mergiraf", !.to1 = "", !.prompt1 = "", !.handed1 = FALSE, !.ran_mergiraf = TRUE]

Claim1MergirafStagedAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "mergiraf"
    /\ s' = [s EXCEPT !.d1 = "staged", !.by1 = "mergiraf", !.to1 = "", !.prompt1 = "", !.handed1 = TRUE, !.ran_mergiraf = TRUE]

Claim1MergirafRefused ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "refused", !.by1 = "mergiraf", !.to1 = "", !.prompt1 = "", !.handed1 = FALSE, !.ran_mergiraf = TRUE]

Claim1MergirafRefusedAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "mergiraf"
    /\ s' = [s EXCEPT !.d1 = "refused", !.by1 = "mergiraf", !.to1 = "", !.prompt1 = "", !.handed1 = TRUE, !.ran_mergiraf = TRUE]

Claim1MergirafDeferredToMergiraf ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "deferred", !.by1 = "mergiraf", !.to1 = "mergiraf", !.prompt1 = "", !.handed1 = FALSE, !.ran_mergiraf = TRUE]

Claim1MergirafDeferredToMergirafAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "mergiraf"
    /\ s' = [s EXCEPT !.d1 = "deferred", !.by1 = "mergiraf", !.to1 = "mergiraf", !.prompt1 = "", !.handed1 = TRUE, !.ran_mergiraf = TRUE]

Claim1MergirafDeferredToPrepare ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "deferred", !.by1 = "mergiraf", !.to1 = "prepare", !.prompt1 = "", !.handed1 = FALSE, !.ran_mergiraf = TRUE]

Claim1MergirafDeferredToPrepareAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "mergiraf"
    /\ s' = [s EXCEPT !.d1 = "deferred", !.by1 = "mergiraf", !.to1 = "prepare", !.prompt1 = "", !.handed1 = TRUE, !.ran_mergiraf = TRUE]

Claim1MergirafDeferredToBundle ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "deferred", !.by1 = "mergiraf", !.to1 = "bundle", !.prompt1 = "", !.handed1 = FALSE, !.ran_mergiraf = TRUE]

Claim1MergirafDeferredToBundleAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "mergiraf"
    /\ s' = [s EXCEPT !.d1 = "deferred", !.by1 = "mergiraf", !.to1 = "bundle", !.prompt1 = "", !.handed1 = TRUE, !.ran_mergiraf = TRUE]

Claim1MergirafToModelMarker ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "to_model", !.by1 = "mergiraf", !.to1 = "", !.prompt1 = "marker", !.handed1 = FALSE, !.ran_mergiraf = TRUE]

Claim1MergirafToModelMarkerAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "mergiraf"
    /\ s' = [s EXCEPT !.d1 = "to_model", !.by1 = "mergiraf", !.to1 = "", !.prompt1 = "marker", !.handed1 = TRUE, !.ran_mergiraf = TRUE]

Claim1MergirafToModelModifyDelete ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "to_model", !.by1 = "mergiraf", !.to1 = "", !.prompt1 = "modify_delete", !.handed1 = FALSE, !.ran_mergiraf = TRUE]

Claim1MergirafToModelModifyDeleteAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "mergiraf"
    /\ s' = [s EXCEPT !.d1 = "to_model", !.by1 = "mergiraf", !.to1 = "", !.prompt1 = "modify_delete", !.handed1 = TRUE, !.ran_mergiraf = TRUE]

Claim1MergirafToModelSidecar ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "to_model", !.by1 = "mergiraf", !.to1 = "", !.prompt1 = "sidecar", !.handed1 = FALSE, !.ran_mergiraf = TRUE]

Claim1MergirafToModelSidecarAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "mergiraf"
    /\ s' = [s EXCEPT !.d1 = "to_model", !.by1 = "mergiraf", !.to1 = "", !.prompt1 = "sidecar", !.handed1 = TRUE, !.ran_mergiraf = TRUE]

Claim1PrepareStaged ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "staged", !.by1 = "prepare", !.to1 = "", !.prompt1 = "", !.handed1 = FALSE, !.ran_prepare = TRUE]

Claim1PrepareStagedAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "prepare"
    /\ s' = [s EXCEPT !.d1 = "staged", !.by1 = "prepare", !.to1 = "", !.prompt1 = "", !.handed1 = TRUE, !.ran_prepare = TRUE]

Claim1PrepareRefused ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "refused", !.by1 = "prepare", !.to1 = "", !.prompt1 = "", !.handed1 = FALSE, !.ran_prepare = TRUE]

Claim1PrepareRefusedAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "prepare"
    /\ s' = [s EXCEPT !.d1 = "refused", !.by1 = "prepare", !.to1 = "", !.prompt1 = "", !.handed1 = TRUE, !.ran_prepare = TRUE]

Claim1PrepareDeferredToMergiraf ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "deferred", !.by1 = "prepare", !.to1 = "mergiraf", !.prompt1 = "", !.handed1 = FALSE, !.ran_prepare = TRUE]

Claim1PrepareDeferredToMergirafAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "prepare"
    /\ s' = [s EXCEPT !.d1 = "deferred", !.by1 = "prepare", !.to1 = "mergiraf", !.prompt1 = "", !.handed1 = TRUE, !.ran_prepare = TRUE]

Claim1PrepareDeferredToPrepare ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "deferred", !.by1 = "prepare", !.to1 = "prepare", !.prompt1 = "", !.handed1 = FALSE, !.ran_prepare = TRUE]

Claim1PrepareDeferredToPrepareAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "prepare"
    /\ s' = [s EXCEPT !.d1 = "deferred", !.by1 = "prepare", !.to1 = "prepare", !.prompt1 = "", !.handed1 = TRUE, !.ran_prepare = TRUE]

Claim1PrepareDeferredToBundle ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "deferred", !.by1 = "prepare", !.to1 = "bundle", !.prompt1 = "", !.handed1 = FALSE, !.ran_prepare = TRUE]

Claim1PrepareDeferredToBundleAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "prepare"
    /\ s' = [s EXCEPT !.d1 = "deferred", !.by1 = "prepare", !.to1 = "bundle", !.prompt1 = "", !.handed1 = TRUE, !.ran_prepare = TRUE]

Claim1PrepareToModelMarker ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "to_model", !.by1 = "prepare", !.to1 = "", !.prompt1 = "marker", !.handed1 = FALSE, !.ran_prepare = TRUE]

Claim1PrepareToModelMarkerAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "prepare"
    /\ s' = [s EXCEPT !.d1 = "to_model", !.by1 = "prepare", !.to1 = "", !.prompt1 = "marker", !.handed1 = TRUE, !.ran_prepare = TRUE]

Claim1PrepareToModelModifyDelete ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "to_model", !.by1 = "prepare", !.to1 = "", !.prompt1 = "modify_delete", !.handed1 = FALSE, !.ran_prepare = TRUE]

Claim1PrepareToModelModifyDeleteAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "prepare"
    /\ s' = [s EXCEPT !.d1 = "to_model", !.by1 = "prepare", !.to1 = "", !.prompt1 = "modify_delete", !.handed1 = TRUE, !.ran_prepare = TRUE]

Claim1PrepareToModelSidecar ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "to_model", !.by1 = "prepare", !.to1 = "", !.prompt1 = "sidecar", !.handed1 = FALSE, !.ran_prepare = TRUE]

Claim1PrepareToModelSidecarAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "prepare"
    /\ s' = [s EXCEPT !.d1 = "to_model", !.by1 = "prepare", !.to1 = "", !.prompt1 = "sidecar", !.handed1 = TRUE, !.ran_prepare = TRUE]

Claim1BundleStaged ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "staged", !.by1 = "bundle", !.to1 = "", !.prompt1 = "", !.handed1 = FALSE, !.ran_bundle = TRUE]

Claim1BundleStagedAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "bundle"
    /\ s' = [s EXCEPT !.d1 = "staged", !.by1 = "bundle", !.to1 = "", !.prompt1 = "", !.handed1 = TRUE, !.ran_bundle = TRUE]

Claim1BundleRefused ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "refused", !.by1 = "bundle", !.to1 = "", !.prompt1 = "", !.handed1 = FALSE, !.ran_bundle = TRUE]

Claim1BundleRefusedAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "bundle"
    /\ s' = [s EXCEPT !.d1 = "refused", !.by1 = "bundle", !.to1 = "", !.prompt1 = "", !.handed1 = TRUE, !.ran_bundle = TRUE]

Claim1BundleDeferredToMergiraf ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "deferred", !.by1 = "bundle", !.to1 = "mergiraf", !.prompt1 = "", !.handed1 = FALSE, !.ran_bundle = TRUE]

Claim1BundleDeferredToMergirafAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "bundle"
    /\ s' = [s EXCEPT !.d1 = "deferred", !.by1 = "bundle", !.to1 = "mergiraf", !.prompt1 = "", !.handed1 = TRUE, !.ran_bundle = TRUE]

Claim1BundleDeferredToPrepare ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "deferred", !.by1 = "bundle", !.to1 = "prepare", !.prompt1 = "", !.handed1 = FALSE, !.ran_bundle = TRUE]

Claim1BundleDeferredToPrepareAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "bundle"
    /\ s' = [s EXCEPT !.d1 = "deferred", !.by1 = "bundle", !.to1 = "prepare", !.prompt1 = "", !.handed1 = TRUE, !.ran_bundle = TRUE]

Claim1BundleDeferredToBundle ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "deferred", !.by1 = "bundle", !.to1 = "bundle", !.prompt1 = "", !.handed1 = FALSE, !.ran_bundle = TRUE]

Claim1BundleDeferredToBundleAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "bundle"
    /\ s' = [s EXCEPT !.d1 = "deferred", !.by1 = "bundle", !.to1 = "bundle", !.prompt1 = "", !.handed1 = TRUE, !.ran_bundle = TRUE]

Claim1BundleToModelMarker ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "to_model", !.by1 = "bundle", !.to1 = "", !.prompt1 = "marker", !.handed1 = FALSE, !.ran_bundle = TRUE]

Claim1BundleToModelMarkerAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "bundle"
    /\ s' = [s EXCEPT !.d1 = "to_model", !.by1 = "bundle", !.to1 = "", !.prompt1 = "marker", !.handed1 = TRUE, !.ran_bundle = TRUE]

Claim1BundleToModelModifyDelete ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "to_model", !.by1 = "bundle", !.to1 = "", !.prompt1 = "modify_delete", !.handed1 = FALSE, !.ran_bundle = TRUE]

Claim1BundleToModelModifyDeleteAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "bundle"
    /\ s' = [s EXCEPT !.d1 = "to_model", !.by1 = "bundle", !.to1 = "", !.prompt1 = "modify_delete", !.handed1 = TRUE, !.ran_bundle = TRUE]

Claim1BundleToModelSidecar ==
    /\ s.d1 = "unclaimed"
    /\ s' = [s EXCEPT !.d1 = "to_model", !.by1 = "bundle", !.to1 = "", !.prompt1 = "sidecar", !.handed1 = FALSE, !.ran_bundle = TRUE]

Claim1BundleToModelSidecarAfterDeferral ==
    /\ s.d1 = "deferred"
    /\ s.to1 = "bundle"
    /\ s' = [s EXCEPT !.d1 = "to_model", !.by1 = "bundle", !.to1 = "", !.prompt1 = "sidecar", !.handed1 = TRUE, !.ran_bundle = TRUE]

Claim2MergirafStaged ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "staged", !.by2 = "mergiraf", !.to2 = "", !.prompt2 = "", !.handed2 = FALSE, !.ran_mergiraf = TRUE]

Claim2MergirafStagedAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "mergiraf"
    /\ s' = [s EXCEPT !.d2 = "staged", !.by2 = "mergiraf", !.to2 = "", !.prompt2 = "", !.handed2 = TRUE, !.ran_mergiraf = TRUE]

Claim2MergirafRefused ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "refused", !.by2 = "mergiraf", !.to2 = "", !.prompt2 = "", !.handed2 = FALSE, !.ran_mergiraf = TRUE]

Claim2MergirafRefusedAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "mergiraf"
    /\ s' = [s EXCEPT !.d2 = "refused", !.by2 = "mergiraf", !.to2 = "", !.prompt2 = "", !.handed2 = TRUE, !.ran_mergiraf = TRUE]

Claim2MergirafDeferredToMergiraf ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "deferred", !.by2 = "mergiraf", !.to2 = "mergiraf", !.prompt2 = "", !.handed2 = FALSE, !.ran_mergiraf = TRUE]

Claim2MergirafDeferredToMergirafAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "mergiraf"
    /\ s' = [s EXCEPT !.d2 = "deferred", !.by2 = "mergiraf", !.to2 = "mergiraf", !.prompt2 = "", !.handed2 = TRUE, !.ran_mergiraf = TRUE]

Claim2MergirafDeferredToPrepare ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "deferred", !.by2 = "mergiraf", !.to2 = "prepare", !.prompt2 = "", !.handed2 = FALSE, !.ran_mergiraf = TRUE]

Claim2MergirafDeferredToPrepareAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "mergiraf"
    /\ s' = [s EXCEPT !.d2 = "deferred", !.by2 = "mergiraf", !.to2 = "prepare", !.prompt2 = "", !.handed2 = TRUE, !.ran_mergiraf = TRUE]

Claim2MergirafDeferredToBundle ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "deferred", !.by2 = "mergiraf", !.to2 = "bundle", !.prompt2 = "", !.handed2 = FALSE, !.ran_mergiraf = TRUE]

Claim2MergirafDeferredToBundleAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "mergiraf"
    /\ s' = [s EXCEPT !.d2 = "deferred", !.by2 = "mergiraf", !.to2 = "bundle", !.prompt2 = "", !.handed2 = TRUE, !.ran_mergiraf = TRUE]

Claim2MergirafToModelMarker ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "to_model", !.by2 = "mergiraf", !.to2 = "", !.prompt2 = "marker", !.handed2 = FALSE, !.ran_mergiraf = TRUE]

Claim2MergirafToModelMarkerAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "mergiraf"
    /\ s' = [s EXCEPT !.d2 = "to_model", !.by2 = "mergiraf", !.to2 = "", !.prompt2 = "marker", !.handed2 = TRUE, !.ran_mergiraf = TRUE]

Claim2MergirafToModelModifyDelete ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "to_model", !.by2 = "mergiraf", !.to2 = "", !.prompt2 = "modify_delete", !.handed2 = FALSE, !.ran_mergiraf = TRUE]

Claim2MergirafToModelModifyDeleteAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "mergiraf"
    /\ s' = [s EXCEPT !.d2 = "to_model", !.by2 = "mergiraf", !.to2 = "", !.prompt2 = "modify_delete", !.handed2 = TRUE, !.ran_mergiraf = TRUE]

Claim2MergirafToModelSidecar ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "to_model", !.by2 = "mergiraf", !.to2 = "", !.prompt2 = "sidecar", !.handed2 = FALSE, !.ran_mergiraf = TRUE]

Claim2MergirafToModelSidecarAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "mergiraf"
    /\ s' = [s EXCEPT !.d2 = "to_model", !.by2 = "mergiraf", !.to2 = "", !.prompt2 = "sidecar", !.handed2 = TRUE, !.ran_mergiraf = TRUE]

Claim2PrepareStaged ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "staged", !.by2 = "prepare", !.to2 = "", !.prompt2 = "", !.handed2 = FALSE, !.ran_prepare = TRUE]

Claim2PrepareStagedAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "prepare"
    /\ s' = [s EXCEPT !.d2 = "staged", !.by2 = "prepare", !.to2 = "", !.prompt2 = "", !.handed2 = TRUE, !.ran_prepare = TRUE]

Claim2PrepareRefused ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "refused", !.by2 = "prepare", !.to2 = "", !.prompt2 = "", !.handed2 = FALSE, !.ran_prepare = TRUE]

Claim2PrepareRefusedAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "prepare"
    /\ s' = [s EXCEPT !.d2 = "refused", !.by2 = "prepare", !.to2 = "", !.prompt2 = "", !.handed2 = TRUE, !.ran_prepare = TRUE]

Claim2PrepareDeferredToMergiraf ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "deferred", !.by2 = "prepare", !.to2 = "mergiraf", !.prompt2 = "", !.handed2 = FALSE, !.ran_prepare = TRUE]

Claim2PrepareDeferredToMergirafAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "prepare"
    /\ s' = [s EXCEPT !.d2 = "deferred", !.by2 = "prepare", !.to2 = "mergiraf", !.prompt2 = "", !.handed2 = TRUE, !.ran_prepare = TRUE]

Claim2PrepareDeferredToPrepare ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "deferred", !.by2 = "prepare", !.to2 = "prepare", !.prompt2 = "", !.handed2 = FALSE, !.ran_prepare = TRUE]

Claim2PrepareDeferredToPrepareAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "prepare"
    /\ s' = [s EXCEPT !.d2 = "deferred", !.by2 = "prepare", !.to2 = "prepare", !.prompt2 = "", !.handed2 = TRUE, !.ran_prepare = TRUE]

Claim2PrepareDeferredToBundle ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "deferred", !.by2 = "prepare", !.to2 = "bundle", !.prompt2 = "", !.handed2 = FALSE, !.ran_prepare = TRUE]

Claim2PrepareDeferredToBundleAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "prepare"
    /\ s' = [s EXCEPT !.d2 = "deferred", !.by2 = "prepare", !.to2 = "bundle", !.prompt2 = "", !.handed2 = TRUE, !.ran_prepare = TRUE]

Claim2PrepareToModelMarker ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "to_model", !.by2 = "prepare", !.to2 = "", !.prompt2 = "marker", !.handed2 = FALSE, !.ran_prepare = TRUE]

Claim2PrepareToModelMarkerAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "prepare"
    /\ s' = [s EXCEPT !.d2 = "to_model", !.by2 = "prepare", !.to2 = "", !.prompt2 = "marker", !.handed2 = TRUE, !.ran_prepare = TRUE]

Claim2PrepareToModelModifyDelete ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "to_model", !.by2 = "prepare", !.to2 = "", !.prompt2 = "modify_delete", !.handed2 = FALSE, !.ran_prepare = TRUE]

Claim2PrepareToModelModifyDeleteAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "prepare"
    /\ s' = [s EXCEPT !.d2 = "to_model", !.by2 = "prepare", !.to2 = "", !.prompt2 = "modify_delete", !.handed2 = TRUE, !.ran_prepare = TRUE]

Claim2PrepareToModelSidecar ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "to_model", !.by2 = "prepare", !.to2 = "", !.prompt2 = "sidecar", !.handed2 = FALSE, !.ran_prepare = TRUE]

Claim2PrepareToModelSidecarAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "prepare"
    /\ s' = [s EXCEPT !.d2 = "to_model", !.by2 = "prepare", !.to2 = "", !.prompt2 = "sidecar", !.handed2 = TRUE, !.ran_prepare = TRUE]

Claim2BundleStaged ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "staged", !.by2 = "bundle", !.to2 = "", !.prompt2 = "", !.handed2 = FALSE, !.ran_bundle = TRUE]

Claim2BundleStagedAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "bundle"
    /\ s' = [s EXCEPT !.d2 = "staged", !.by2 = "bundle", !.to2 = "", !.prompt2 = "", !.handed2 = TRUE, !.ran_bundle = TRUE]

Claim2BundleRefused ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "refused", !.by2 = "bundle", !.to2 = "", !.prompt2 = "", !.handed2 = FALSE, !.ran_bundle = TRUE]

Claim2BundleRefusedAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "bundle"
    /\ s' = [s EXCEPT !.d2 = "refused", !.by2 = "bundle", !.to2 = "", !.prompt2 = "", !.handed2 = TRUE, !.ran_bundle = TRUE]

Claim2BundleDeferredToMergiraf ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "deferred", !.by2 = "bundle", !.to2 = "mergiraf", !.prompt2 = "", !.handed2 = FALSE, !.ran_bundle = TRUE]

Claim2BundleDeferredToMergirafAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "bundle"
    /\ s' = [s EXCEPT !.d2 = "deferred", !.by2 = "bundle", !.to2 = "mergiraf", !.prompt2 = "", !.handed2 = TRUE, !.ran_bundle = TRUE]

Claim2BundleDeferredToPrepare ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "deferred", !.by2 = "bundle", !.to2 = "prepare", !.prompt2 = "", !.handed2 = FALSE, !.ran_bundle = TRUE]

Claim2BundleDeferredToPrepareAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "bundle"
    /\ s' = [s EXCEPT !.d2 = "deferred", !.by2 = "bundle", !.to2 = "prepare", !.prompt2 = "", !.handed2 = TRUE, !.ran_bundle = TRUE]

Claim2BundleDeferredToBundle ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "deferred", !.by2 = "bundle", !.to2 = "bundle", !.prompt2 = "", !.handed2 = FALSE, !.ran_bundle = TRUE]

Claim2BundleDeferredToBundleAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "bundle"
    /\ s' = [s EXCEPT !.d2 = "deferred", !.by2 = "bundle", !.to2 = "bundle", !.prompt2 = "", !.handed2 = TRUE, !.ran_bundle = TRUE]

Claim2BundleToModelMarker ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "to_model", !.by2 = "bundle", !.to2 = "", !.prompt2 = "marker", !.handed2 = FALSE, !.ran_bundle = TRUE]

Claim2BundleToModelMarkerAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "bundle"
    /\ s' = [s EXCEPT !.d2 = "to_model", !.by2 = "bundle", !.to2 = "", !.prompt2 = "marker", !.handed2 = TRUE, !.ran_bundle = TRUE]

Claim2BundleToModelModifyDelete ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "to_model", !.by2 = "bundle", !.to2 = "", !.prompt2 = "modify_delete", !.handed2 = FALSE, !.ran_bundle = TRUE]

Claim2BundleToModelModifyDeleteAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "bundle"
    /\ s' = [s EXCEPT !.d2 = "to_model", !.by2 = "bundle", !.to2 = "", !.prompt2 = "modify_delete", !.handed2 = TRUE, !.ran_bundle = TRUE]

Claim2BundleToModelSidecar ==
    /\ s.d2 = "unclaimed"
    /\ s' = [s EXCEPT !.d2 = "to_model", !.by2 = "bundle", !.to2 = "", !.prompt2 = "sidecar", !.handed2 = FALSE, !.ran_bundle = TRUE]

Claim2BundleToModelSidecarAfterDeferral ==
    /\ s.d2 = "deferred"
    /\ s.to2 = "bundle"
    /\ s' = [s EXCEPT !.d2 = "to_model", !.by2 = "bundle", !.to2 = "", !.prompt2 = "sidecar", !.handed2 = TRUE, !.ran_bundle = TRUE]

Next ==
    \/ Claim1MergirafStaged
    \/ Claim1MergirafStagedAfterDeferral
    \/ Claim1MergirafRefused
    \/ Claim1MergirafRefusedAfterDeferral
    \/ Claim1MergirafDeferredToMergiraf
    \/ Claim1MergirafDeferredToMergirafAfterDeferral
    \/ Claim1MergirafDeferredToPrepare
    \/ Claim1MergirafDeferredToPrepareAfterDeferral
    \/ Claim1MergirafDeferredToBundle
    \/ Claim1MergirafDeferredToBundleAfterDeferral
    \/ Claim1MergirafToModelMarker
    \/ Claim1MergirafToModelMarkerAfterDeferral
    \/ Claim1MergirafToModelModifyDelete
    \/ Claim1MergirafToModelModifyDeleteAfterDeferral
    \/ Claim1MergirafToModelSidecar
    \/ Claim1MergirafToModelSidecarAfterDeferral
    \/ Claim1PrepareStaged
    \/ Claim1PrepareStagedAfterDeferral
    \/ Claim1PrepareRefused
    \/ Claim1PrepareRefusedAfterDeferral
    \/ Claim1PrepareDeferredToMergiraf
    \/ Claim1PrepareDeferredToMergirafAfterDeferral
    \/ Claim1PrepareDeferredToPrepare
    \/ Claim1PrepareDeferredToPrepareAfterDeferral
    \/ Claim1PrepareDeferredToBundle
    \/ Claim1PrepareDeferredToBundleAfterDeferral
    \/ Claim1PrepareToModelMarker
    \/ Claim1PrepareToModelMarkerAfterDeferral
    \/ Claim1PrepareToModelModifyDelete
    \/ Claim1PrepareToModelModifyDeleteAfterDeferral
    \/ Claim1PrepareToModelSidecar
    \/ Claim1PrepareToModelSidecarAfterDeferral
    \/ Claim1BundleStaged
    \/ Claim1BundleStagedAfterDeferral
    \/ Claim1BundleRefused
    \/ Claim1BundleRefusedAfterDeferral
    \/ Claim1BundleDeferredToMergiraf
    \/ Claim1BundleDeferredToMergirafAfterDeferral
    \/ Claim1BundleDeferredToPrepare
    \/ Claim1BundleDeferredToPrepareAfterDeferral
    \/ Claim1BundleDeferredToBundle
    \/ Claim1BundleDeferredToBundleAfterDeferral
    \/ Claim1BundleToModelMarker
    \/ Claim1BundleToModelMarkerAfterDeferral
    \/ Claim1BundleToModelModifyDelete
    \/ Claim1BundleToModelModifyDeleteAfterDeferral
    \/ Claim1BundleToModelSidecar
    \/ Claim1BundleToModelSidecarAfterDeferral
    \/ Claim2MergirafStaged
    \/ Claim2MergirafStagedAfterDeferral
    \/ Claim2MergirafRefused
    \/ Claim2MergirafRefusedAfterDeferral
    \/ Claim2MergirafDeferredToMergiraf
    \/ Claim2MergirafDeferredToMergirafAfterDeferral
    \/ Claim2MergirafDeferredToPrepare
    \/ Claim2MergirafDeferredToPrepareAfterDeferral
    \/ Claim2MergirafDeferredToBundle
    \/ Claim2MergirafDeferredToBundleAfterDeferral
    \/ Claim2MergirafToModelMarker
    \/ Claim2MergirafToModelMarkerAfterDeferral
    \/ Claim2MergirafToModelModifyDelete
    \/ Claim2MergirafToModelModifyDeleteAfterDeferral
    \/ Claim2MergirafToModelSidecar
    \/ Claim2MergirafToModelSidecarAfterDeferral
    \/ Claim2PrepareStaged
    \/ Claim2PrepareStagedAfterDeferral
    \/ Claim2PrepareRefused
    \/ Claim2PrepareRefusedAfterDeferral
    \/ Claim2PrepareDeferredToMergiraf
    \/ Claim2PrepareDeferredToMergirafAfterDeferral
    \/ Claim2PrepareDeferredToPrepare
    \/ Claim2PrepareDeferredToPrepareAfterDeferral
    \/ Claim2PrepareDeferredToBundle
    \/ Claim2PrepareDeferredToBundleAfterDeferral
    \/ Claim2PrepareToModelMarker
    \/ Claim2PrepareToModelMarkerAfterDeferral
    \/ Claim2PrepareToModelModifyDelete
    \/ Claim2PrepareToModelModifyDeleteAfterDeferral
    \/ Claim2PrepareToModelSidecar
    \/ Claim2PrepareToModelSidecarAfterDeferral
    \/ Claim2BundleStaged
    \/ Claim2BundleStagedAfterDeferral
    \/ Claim2BundleRefused
    \/ Claim2BundleRefusedAfterDeferral
    \/ Claim2BundleDeferredToMergiraf
    \/ Claim2BundleDeferredToMergirafAfterDeferral
    \/ Claim2BundleDeferredToPrepare
    \/ Claim2BundleDeferredToPrepareAfterDeferral
    \/ Claim2BundleDeferredToBundle
    \/ Claim2BundleDeferredToBundleAfterDeferral
    \/ Claim2BundleToModelMarker
    \/ Claim2BundleToModelMarkerAfterDeferral
    \/ Claim2BundleToModelModifyDelete
    \/ Claim2BundleToModelModifyDeleteAfterDeferral
    \/ Claim2BundleToModelSidecar
    \/ Claim2BundleToModelSidecarAfterDeferral

\* Every field stays within its declared domain -- a structural check on the
\* generated updates, not a restatement of anything Python already proves.
TypeOK == s \in AllStates

\* One path's entry, read by path index.
Disp == [i \in 1..N |-> CASE i = 1 -> s.d1 [] i = 2 -> s.d2]
By == [i \in 1..N |-> CASE i = 1 -> s.by1 [] i = 2 -> s.by2]
To == [i \in 1..N |-> CASE i = 1 -> s.to1 [] i = 2 -> s.to2]
Prompt == [i \in 1..N |-> CASE i = 1 -> s.prompt1 [] i = 2 -> s.prompt2]
Handed == [i \in 1..N |-> CASE i = 1 -> s.handed1 [] i = 2 -> s.handed2]

\* The last word on a path: `claim` refuses a second claim on one of these.
Terminal == {"staged", "refused", "to_model"}

Partition(c) == { i \in 1..N : Disp[i] = c }

Unclaimed == Partition("unclaimed")
Staged == Partition("staged")
Deferred == Partition("deferred")
Refused == Partition("refused")
ToModel == Partition("to_model")

\* The property the twenty bash arrays did NOT have: a path could sit in
\* `llm_list` and `deferred_regen` at once, and nothing said which pass owned
\* it.  One disposition field per path makes that overlap unrepresentable, so
\* this holds by construction -- and the construction IS the fix.  It reds if a
\* later edit gives a path a second way to be in a partition.
DisjointPartitions ==
    /\ Unclaimed \cap Staged = {}
    /\ Unclaimed \cap Deferred = {}
    /\ Unclaimed \cap Refused = {}
    /\ Unclaimed \cap ToModel = {}
    /\ Staged \cap Deferred = {}
    /\ Staged \cap Refused = {}
    /\ Staged \cap ToModel = {}
    /\ Deferred \cap Refused = {}
    /\ Deferred \cap ToModel = {}
    /\ Refused \cap ToModel = {}
    /\ Unclaimed \cup Staged \cup Deferred \cup Refused \cup ToModel = 1..N

\* Each argument field belongs to exactly one state, and its state requires it.
\* That is `Disposition.__post_init__`'s rule, checked here over every state the
\* generated updates can reach rather than at one constructor call.
FieldsOwnedByState ==
    /\ Deferred = { i \in 1..N : To[i] # "" }
    /\ ToModel = { i \in 1..N : Prompt[i] # "" }
    /\ Unclaimed = { i \in 1..N : By[i] = "" }

Inv ==
    /\ TypeOK
    /\ DisjointPartitions
    /\ FieldsOwnedByState

\* No lost path: a claimed path never goes back to unclaimed.  The other half of
\* that claim -- a path never leaves the ledger -- is DisjointPartitions' last
\* line, which holds in every state.
NeverUnclaimedAgain ==
    /\ [][ s.d1 # "unclaimed" => s'.d1 # "unclaimed" ]_s
    /\ [][ s.d2 # "unclaimed" => s'.d2 # "unclaimed" ]_s

\* Single claim: a terminal entry never changes again, so a second pass can
\* never disagree with the pass that had the last word.
TerminalClaimIsFinal ==
    /\ [][ s.d1 \in Terminal => (s'.d1 = s.d1 /\ s'.by1 = s.by1 /\ s'.prompt1 = s.prompt1) ]_s
    /\ [][ s.d2 \in Terminal => (s'.d2 = s.d2 /\ s'.by2 = s.by2 /\ s'.prompt2 = s.prompt2) ]_s

ClaimsAreFinal == NeverUnclaimedAgain /\ TerminalClaimIsFinal

AllClaimsTerminal == \A i \in 1..N : Disp[i] \in Terminal

\* Nothing is left to claim: from a ledger whose every path is terminal, no step
\* changes anything.  Stated over TERMINAL and not over "claimed", because a
\* DEFERRED path IS claimed and still has one claim to come -- the witness in
\* ConflictLedger_handoff is that counterexample.
NoClaimOnceSettled == [][ AllClaimsTerminal => UNCHANGED s ]_s

\* Non-vacuity witnesses (EXPECT-EXIT 12 in the .cfg): TLC's counterexample
\* trace over the negation IS the reachability proof.

\* A deferral really is finished by the pass it names.  `handed` is written only
\* by a claim whose guard read `to = <this pass>`, so a handed STAGED path is one
\* the deferral's named pass came back for.
NoHandoff == ~(\E i \in 1..N : Handed[i] /\ Disp[i] = "staged")

AllPassesRan ==
    /\ s.ran_mergiraf
    /\ s.ran_prepare
    /\ s.ran_bundle

\* A path nobody judged, after every pass has claimed something.  This is the
\* state `require_fully_dispositioned` refuses, so the trace is what proves that
\* refusal is not vacuous.
NoStuckPath == ~(AllPassesRan /\ Unclaimed # {})

=============================================================================
