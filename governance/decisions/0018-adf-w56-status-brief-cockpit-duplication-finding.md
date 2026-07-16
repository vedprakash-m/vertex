# ADR-0018: Status, Brief and Cockpit Responsibilities

**Scope:** Public architectural rationale, not operational evidence.

Keep specialist command surfaces unless duplicated computation or inconsistent
output demonstrates a need for consolidation. The cockpit composes and explains;
it does not automatically replace the status or brief commands.

Presentation consolidation and canonical risk-state authority are separate
questions. A synchronous YAML/fact dual write does not prove atomic recovery
or permanent consistency: validate the selected workspace and actual read path
before accepting an authority change.

Deployment record counts, source-of-record selections and observed consistency
results belong in private evidence. This document does not certify a live
program's consistency or accept an unmeasured recovery risk.
