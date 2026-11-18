from __future__ import annotations

import hashlib
import json

from src.core.selection_identity import SELECTION_IDENTITY_VERSION, UNVERIFIED, selection_identity_v1


def test_selection_identity_is_order_insensitive_canonical_sha256() -> None:
    records = [("ado", "b", "2" * 64), ("teams", "a", "1" * 64)]
    payload = json.dumps(
        [SELECTION_IDENTITY_VERSION, sorted([list(record) for record in records])], separators=(",", ":"),
    )

    identity = selection_identity_v1(records)

    assert identity == "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert selection_identity_v1(reversed(records)) == identity


def test_selection_identity_changes_with_any_record_field() -> None:
    base = selection_identity_v1([("ado", "a", "0" * 64)])
    assert selection_identity_v1([("teams", "a", "0" * 64)]) != base
    assert selection_identity_v1([("ado", "b", "0" * 64)]) != base
    assert selection_identity_v1([("ado", "a", "1" * 64)]) != base
    assert selection_identity_v1([]) != base
    assert UNVERIFIED == "unverified"
