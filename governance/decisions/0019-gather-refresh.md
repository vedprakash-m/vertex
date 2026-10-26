# ADR-0019: Governed Gather Refresh

**Status:** Public contract summary; not a deployment approval.

Gather uses explicit program scope, immutable run manifests and a fenced
workspace lease. A run is not current merely because an attempt completed:
readers must verify the committed manifest and its required scope.

Each deployment must privately record its source permissions, query bindings,
freshness policy, retention, recovery objectives, operating identity, alert
route and accountable owner before enabling recurring execution. This document
does not authorize a task, credential, waiver or external data access.

Use the [manual gather procedure](../runbooks/manual-gather-runbook.md) and
[scheduled tasks procedure](../runbooks/scheduled-tasks-runbook.md) as generic
operating guidance. Deployment-specific decisions, identifiers, measurements
and execution evidence belong in private operator storage, not this repository.
