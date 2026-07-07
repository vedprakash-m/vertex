# ADR-0004: Recorded-Time-Travel Fact Store (Not Full Bitemporal)

**Date:** 2026-06-16
**Status:** Accepted
**Workstream:** SD-15 (spec label correction)
**Author(s):** Vertex engineering
**Approver(s):** n/a

## Context

The Program Fact Store schema (`program_fact_revisions` table) stores `valid_from`, `valid_until`, and
`recorded_at` columns. Internal documentation and the tech spec used the term "bitemporal" to describe
this, implying that both the transaction-time axis (`recorded_at`) and the valid-time axis
(`valid_from`/`valid_until`) are query-first-class. In practice, `as_of` queries filter on
`recorded_at` only; `valid_from`/`valid_until` are stored for provenance but are not yet a query
axis. This distinction matters for consumers who would expect `as_of=2026-01-01` to return facts that
were *valid* then (bitemporal) vs. facts that were *recorded* by then (recorded-time-travel).

## Decision

The term "bitemporal" is replaced with "recorded-time-travel" throughout the tech spec (§9.16). The
full valid-time axis is deferred to GAP-36d. The columns `valid_from` / `valid_until` are retained in
the schema to avoid a migration, and their semantics are documented as "stored for provenance; not yet
a query axis." GAP-36d tracks the future implementation of valid-time queries.

## Consequences

Easier: no schema change required; consumers have accurate expectations about what `as_of` returns.
Harder: future implementation of full bitemporal queries requires a GAP-36d release and a schema
version bump. Explicitly rejected: silently keeping "bitemporal" (misleads future query authors).

## References

- `specs/vertex-tech-spec.md §9.16` — Program Fact Store
- `specs/gaps.md GAP-36d` — full bitemporal (valid-time) query axis
- `src/core/program_fact_store.py` — schema and `as_of` query implementation
