# Changelog

## 2026-11-03

| Change | Behavior |
|---|---|
| Draft evidence | Capture effective source selectors; refuse ordinary confirmation of unbound or selector-drifted drafts, including forced confirmation. Historical reads remain available. |
| Metric lineage | Preserve observed units and semantic-definition fingerprints across persistence, compaction and histories. Incompatible or unknown trends cannot imply recovery. |
| Context inventory | Expose metadata-only authored review-rule coverage without changing review thresholds or publication policy. |
| Publication privacy | Ignore raw source-document and credential-container exports; make the model-card example explicitly synthetic. |

Modified components: report/confirm capture and stages; metric projection, store,
reconciliation, evaluation and history commands; program inventory; publication
ignore rules and model-card guidance; canonical requirements, technical and UX
specifications; citation verification.

Tests: capture/report/confirm and archive-locked drift; metric projection,
store/correction/compaction, reconciliation, evaluation and histories; inventory
classification/containment; publication ignore contracts.

The generated CLI reference now reflects optional command-group invocation.
Existing draft-validation and additive-schema helpers were relocated to retain
the unchanged command/store size ceilings.

These are bounded implementation changes, not operational qualification,
private-data migration, complete context coverage or whole-run atomicity.
