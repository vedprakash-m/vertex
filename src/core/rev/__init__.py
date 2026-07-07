"""Program-Context Intelligence (REV) — capability-port pipeline.

This package is the Zone-A home for the REV retrieval pipeline defined in
``specs/program-context-intelligence.md``. It holds only deterministic,
provider-agnostic contracts: the capability ports, the three-stage identity
model, the result union, the query planner, the multi-budget governor, the
durable run-state machine, the privacy/scanning lifecycle, the normalizer/
chunker, the layered verifier, delta sync-state, and telemetry. No provider
SDKs and no ``src.ai``/``src.m365``/``src.commands`` imports are permitted
here (INV-1, ``tests/contracts/test_import_boundaries.py``). Zone-C
implementations (``src/m365/rev/``) and Zone-B extraction (``src/ai/rev/``)
adapt these ports to live surfaces.

P1 modules: ports, entity_types, identity, query_planner, governor, run_state,
            privacy, prompt_shields, normalizer, pipeline, health, result.
P2 modules: sync_state (delta sync-state store with TTL/LRU eviction).
"""

from __future__ import annotations