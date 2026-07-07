# Activation Vision Reconciliation Plan

**Scope:** Bar C / P15, including Kusto/IcM reconciliation, GAP-36 conflict adjudication, and GAP-37 EXPLAIN drill-down.

## Source Nodes

The Vision-realized reconciliation proof must compare all four source nodes:

- `ado`
- `eml`
- `kusto`
- `icm`

ADO and EML remain the v1 activation conflict pair. Kusto and IcM join only for Bar C fleet/vision proof and must not block the first-benefit v1 slice.

## Conflict Contract

- Each compared source observation carries an `as_of` timestamp.
- Materiality policy decides whether a discrepancy is actionable or noise.
- A material contradiction emits `disputed`, never a silent overwrite.
- The operator adjudication queue records the decision, rationale, and source lineage keys.

## EXPLAIN Drill-Down

The operator-facing drill-down must show:

- the source excerpt that produced the claim;
- the counter-source context that disagrees;
- lineage keys such as `source_document_key`, `approval_event_id`, query or incident reference, and `as_of`;
- the available operator action: accept, edit, reject, revoke, or defer;
- accessibility review notes for badges, contrast, keyboard flow, and screen-reader text.

## Readiness Rule

Do not claim AG-8/Bar-C vision realization until a 3-program soak demonstrates Kusto/IcM reconciliation and EXPLAIN drill-down on real evidence. This plan is a scaffold, not proof that the live reconciliation has run.
