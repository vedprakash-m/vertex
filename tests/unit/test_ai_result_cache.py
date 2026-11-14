"""ADF-W5.2: src/core/ai_result_cache.py."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.core.ai_result_cache import (
    AIResultCacheKey,
    canonical_input_hash,
    get_ai_result,
    put_ai_result,
)
from src.core.exceptions import StateError
from src.core.rev.rev_cache_store import CACHE_SCHEMA_VERSION, get_cached, put_cached


def _key(**overrides: str) -> AIResultCacheKey:
    defaults = dict(
        program_id="samplealpha",
        feature="risk_proposal_generator",
        canonical_input_hash=canonical_input_hash("some canonical input text"),
        prompt_version="risk_proposal.v1",
        policy_version="policy-2026-07-13",
        model_deployment="gpt-5.4-mini",
        context_manifest_hash="manifest-abc123",
        output_schema_version="1",
    )
    defaults.update(overrides)
    return AIResultCacheKey(**defaults)  # type: ignore[arg-type]


def test_miss_when_nothing_cached(tmp_path: Path) -> None:
    assert get_ai_result(_key(), programs_root=tmp_path) is None


def test_put_then_get_round_trips(tmp_path: Path) -> None:
    key = _key()
    put_ai_result(key, {"causal_title": "Vendor delay"}, programs_root=tmp_path)
    hit = get_ai_result(key, programs_root=tmp_path)
    assert hit is not None
    assert hit.value == {"causal_title": "Vendor delay"}
    assert hit.was_cached is True
    assert hit.model_deployment == "gpt-5.4-mini"


def test_different_program_id_is_a_miss_no_cross_program_reuse(tmp_path: Path) -> None:
    put_ai_result(_key(program_id="samplealpha"), {"x": 1}, programs_root=tmp_path)
    assert get_ai_result(_key(program_id="samplefleet"), programs_root=tmp_path) is None


def test_different_canonical_input_hash_is_a_miss(tmp_path: Path) -> None:
    put_ai_result(_key(canonical_input_hash="hash-a"), {"x": 1}, programs_root=tmp_path)
    assert get_ai_result(_key(canonical_input_hash="hash-b"), programs_root=tmp_path) is None


def test_prompt_version_bump_invalidates_cache(tmp_path: Path) -> None:
    put_ai_result(_key(prompt_version="v1"), {"x": 1}, programs_root=tmp_path)
    assert get_ai_result(_key(prompt_version="v2"), programs_root=tmp_path) is None


def test_policy_version_bump_invalidates_cache(tmp_path: Path) -> None:
    put_ai_result(_key(policy_version="p1"), {"x": 1}, programs_root=tmp_path)
    assert get_ai_result(_key(policy_version="p2"), programs_root=tmp_path) is None


def test_output_schema_version_bump_invalidates_cache(tmp_path: Path) -> None:
    put_ai_result(_key(output_schema_version="1"), {"x": 1}, programs_root=tmp_path)
    assert get_ai_result(_key(output_schema_version="2"), programs_root=tmp_path) is None


@pytest.mark.parametrize("strict_read", [False, True])
def test_shared_cache_distinguishes_delimited_key_components(
    tmp_path: Path, strict_read: bool,
) -> None:
    first = _key(feature="risk|variant", canonical_input_hash="input")
    second = _key(feature="risk", canonical_input_hash="variant|input")
    first_path = put_ai_result(first, {"result": "first"}, programs_root=tmp_path)

    assert get_ai_result(second, programs_root=tmp_path, strict_read=strict_read) is None
    second_path = put_ai_result(second, {"result": "second"}, programs_root=tmp_path)

    assert first_path != second_path
    first_hit = get_ai_result(first, programs_root=tmp_path, strict_read=strict_read)
    second_hit = get_ai_result(second, programs_root=tmp_path, strict_read=strict_read)
    assert first_hit is not None and first_hit.value == {"result": "first"}
    assert second_hit is not None and second_hit.value == {"result": "second"}


def test_shared_cache_distinguishes_empty_key_tuples(tmp_path: Path) -> None:
    options = dict(program_id="sample", kind="synthetic", programs_root=tmp_path, now_epoch=100.0)
    first_path = put_cached(key_values=(), key_labels=(), payload="empty", **options)

    assert get_cached(key_values=("",), ttl_days=90, **options) is None
    second_path = put_cached(key_values=("",), key_labels=("value",), payload="blank", **options)

    assert first_path != second_path
    assert get_cached(key_values=(), ttl_days=90, **options) == "empty"
    assert get_cached(key_values=("",), ttl_days=90, **options) == "blank"


@pytest.mark.parametrize("strict_read", [False, True])
def test_shared_cache_preserves_ordinary_legacy_key_hit(
    tmp_path: Path, strict_read: bool,
) -> None:
    values = ("first", "second")
    labels = ("first", "second")
    path = put_cached(
        program_id="sample", kind="synthetic", key_values=values, key_labels=labels,
        payload="value", programs_root=tmp_path, now_epoch=100.0,
    )
    expected_name = hashlib.sha256("|".join(values).encode()).hexdigest()[:32] + ".json"
    assert path.name == expected_name

    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["key"] = {"second": "second", "first": "first"}
    path.write_text(json.dumps(entry), encoding="utf-8")
    assert get_cached(
        program_id="sample", kind="synthetic", key_values=values, key_labels=labels,
        programs_root=tmp_path, ttl_days=90, now_epoch=101.0, strict_read=strict_read,
    ) == "value"
    updated = put_cached(
        program_id="sample", kind="synthetic", key_values=values, key_labels=labels,
        payload="updated", programs_root=tmp_path, now_epoch=102.0,
    )
    assert updated == path
    assert get_cached(
        program_id="sample", kind="synthetic", key_values=values, key_labels=labels,
        programs_root=tmp_path, ttl_days=90, now_epoch=103.0, strict_read=strict_read,
    ) == "updated"


@pytest.mark.parametrize("strict_read", [False, True])
def test_shared_cache_leaves_ambiguous_legacy_entry_unread(
    tmp_path: Path, strict_read: bool,
) -> None:
    values = ("first|middle", "last")
    labels = ("first", "second")
    cache_dir = tmp_path / "sample" / "rev_cache" / "synthetic"
    cache_dir.mkdir(parents=True)
    legacy = cache_dir / (hashlib.sha256("|".join(values).encode()).hexdigest()[:32] + ".json")
    legacy.write_text(json.dumps({
        "schema_version": CACHE_SCHEMA_VERSION,
        "kind": "synthetic",
        "key": dict(zip(labels, values, strict=True)),
        "captured_at_epoch": 100.0,
        "accessed_at_epoch": 100.0,
        "payload": "old",
    }), encoding="utf-8")
    before = legacy.read_bytes()
    options = dict(program_id="sample", kind="synthetic", key_values=values, key_labels=labels,
                   programs_root=tmp_path, now_epoch=101.0)

    assert get_cached(ttl_days=90, strict_read=strict_read, **options) is None
    assert legacy.read_bytes() == before
    new_path = put_cached(payload="new", **options)
    assert new_path != legacy
    assert legacy.read_bytes() == before
    assert get_cached(ttl_days=90, strict_read=strict_read, **options) == "new"


def test_shared_cache_does_not_touch_or_overwrite_foreign_legacy_identity(tmp_path: Path) -> None:
    old_values = ("first|middle", "last")
    requested_values = ("first", "middle", "last")
    cache_dir = tmp_path / "sample" / "rev_cache" / "synthetic"
    cache_dir.mkdir(parents=True)
    legacy = cache_dir / (hashlib.sha256("|".join(old_values).encode()).hexdigest()[:32] + ".json")
    legacy.write_text(json.dumps({
        "schema_version": CACHE_SCHEMA_VERSION,
        "kind": "synthetic",
        "key": {"first": old_values[0], "second": old_values[1]},
        "captured_at_epoch": 100.0,
        "accessed_at_epoch": 100.0,
        "payload": "foreign",
    }), encoding="utf-8")
    before = legacy.read_bytes()

    options = dict(
        program_id="sample", kind="synthetic", key_values=requested_values,
        key_labels=("first", "second", "third"), programs_root=tmp_path, now_epoch=101.0,
    )
    assert get_cached(ttl_days=90, **options) is None
    assert legacy.read_bytes() == before
    assert get_cached(ttl_days=90, **{**options, "now_epoch": 100.0 + 91 * 86400}) is None
    assert legacy.read_bytes() == before
    with pytest.raises(StateError, match="invalid metadata"):
        get_cached(ttl_days=90, strict_read=True, **options)
    assert legacy.read_bytes() == before

    path = put_cached(payload="requested", **options)
    assert path != legacy
    assert legacy.read_bytes() == before
    assert get_cached(ttl_days=90, **options) == "requested"
    assert get_cached(ttl_days=90, strict_read=True, **options) == "requested"
    assert put_cached(payload="updated", **options) == path
    assert get_cached(ttl_days=90, **options) == "updated"
    assert legacy.read_bytes() == before
    path.unlink()
    assert get_cached(ttl_days=90, **options) is None
    assert legacy.read_bytes() == before


@pytest.mark.parametrize(
    ("values", "labels"),
    [
        (("first", "second"), ("same", "same")),
        (("first", "second"), ("first",)),
        (("first",), ("first", "second")),
    ],
)
@pytest.mark.parametrize("operation", ["read", "strict_read", "write"])
def test_shared_cache_rejects_invalid_labels_before_filesystem_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, values: tuple[str, ...],
    labels: tuple[str, ...], operation: str,
) -> None:
    root = tmp_path / "not-created"
    options = dict(
        program_id="sample", kind="synthetic", key_values=values, key_labels=labels,
        programs_root=root,
    )

    def reject_access(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid cache labels reached the filesystem")

    with monkeypatch.context() as patch:
        for method in ("exists", "lstat", "mkdir", "read_text", "write_text"):
            patch.setattr(Path, method, reject_access)
        with pytest.raises(ValueError, match="key labels"):
            if operation == "write":
                put_cached(payload="value", **options)
            else:
                get_cached(ttl_days=90, strict_read=operation == "strict_read", **options)
    assert not root.exists()


@pytest.mark.parametrize("strict_read", [False, True])
def test_shared_cache_duplicate_labels_cannot_match_foreign_legacy_entry(
    tmp_path: Path, strict_read: bool,
) -> None:
    old_values = ("first|second", "middle", "last")
    cache_dir = tmp_path / "sample" / "rev_cache" / "synthetic"
    cache_dir.mkdir(parents=True)
    legacy = cache_dir / (hashlib.sha256("|".join(old_values).encode()).hexdigest()[:32] + ".json")
    legacy.write_text(json.dumps({
        "schema_version": CACHE_SCHEMA_VERSION,
        "kind": "synthetic",
        "key": {"duplicate": "last", "middle": "middle"},
        "captured_at_epoch": 100.0,
        "accessed_at_epoch": 100.0,
        "payload": "foreign",
    }), encoding="utf-8")
    before = legacy.read_bytes()
    options = dict(
        program_id="sample", kind="synthetic",
        key_values=("first", "second", "middle", "last"),
        key_labels=("duplicate", "duplicate", "middle", "duplicate"),
        programs_root=tmp_path, now_epoch=101.0,
    )

    with pytest.raises(ValueError, match="key labels"):
        get_cached(ttl_days=90, strict_read=strict_read, **options)
    assert legacy.read_bytes() == before
    with pytest.raises(ValueError, match="key labels"):
        put_cached(payload="requested", **options)
    assert legacy.read_bytes() == before


def test_shared_cache_accepts_empty_labeled_and_unlabeled_keys(tmp_path: Path) -> None:
    options = dict(
        program_id="sample", kind="synthetic", key_values=(), programs_root=tmp_path,
        now_epoch=100.0,
    )
    put_cached(key_labels=(), payload="empty", **options)
    assert get_cached(ttl_days=90, **options) == "empty"
    assert get_cached(key_labels=(), ttl_days=90, strict_read=True, **options) == "empty"


def test_canonical_input_hash_is_stable_sha256() -> None:
    a = canonical_input_hash("same text")
    b = canonical_input_hash("same text")
    c = canonical_input_hash("different text")
    assert a == b
    assert a != c
    assert len(a) == 64  # sha256 hex digest


def test_dependency_release_cache_strict_read_does_not_touch_or_prune(tmp_path: Path) -> None:
    key = _key()
    path = put_ai_result(key, {"x": 1}, programs_root=tmp_path, set_at_epoch=100.0, now_epoch=100.0)
    original = path.read_bytes()

    hit = get_ai_result(key, programs_root=tmp_path, now_epoch=101.0, strict_read=True)
    assert hit is not None and hit.value == {"x": 1}
    assert path.read_bytes() == original
    assert get_ai_result(key, programs_root=tmp_path, now_epoch=100.0 + 8 * 86400, strict_read=True) is None
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "damage", [
        "json", "unicode", "schema", "kind", "key", "capture", "capture_bool", "capture_huge",
        "access", "access_inf", "outer_payload", "null_payload", "payload", "version", "model", "missing_model",
    ],
)
def test_dependency_release_cache_strict_read_rejects_bad_metadata(
    tmp_path: Path, damage: str,
) -> None:
    key = _key()
    path = put_ai_result(key, {"x": 1}, programs_root=tmp_path, set_at_epoch=100.0, now_epoch=100.0)
    data = json.loads(path.read_text(encoding="utf-8"))
    if damage == "json":
        path.write_text('{"private-example":', encoding="utf-8")
    elif damage == "unicode":
        path.write_bytes(b"\xff")
    else:
        if damage == "schema":
            data["schema_version"] = "wrong"
        elif damage == "kind":
            data["kind"] = "wrong"
        elif damage == "key":
            data["key"]["program_id"] = "wrong"
        elif damage == "capture":
            data["captured_at_epoch"] = float("nan")
        elif damage == "capture_bool":
            data["captured_at_epoch"] = True
        elif damage == "capture_huge":
            data["captured_at_epoch"] = 10 ** 400
        elif damage == "access":
            data["accessed_at_epoch"] = True
        elif damage == "access_inf":
            data["accessed_at_epoch"] = float("inf")
        elif damage == "outer_payload":
            del data["payload"]
        elif damage == "null_payload":
            data["payload"] = None
        elif damage == "payload":
            del data["payload"]["value"]
        elif damage == "version":
            data["payload"]["policy_version"] = "wrong"
        elif damage == "missing_model":
            del data["payload"]["model_deployment"]
        else:
            data["payload"]["model_deployment"] = "wrong"
        path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(StateError) as raised:
        get_ai_result(key, programs_root=tmp_path, now_epoch=101.0, strict_read=True)
    assert "private-example" not in str(raised.value)
    assert path.read_bytes() == before


def test_dependency_release_cache_strict_absence_is_a_read_only_miss(tmp_path: Path) -> None:
    root = tmp_path / "not-created"
    assert get_ai_result(_key(), programs_root=root, strict_read=True) is None
    assert not root.exists()


@pytest.mark.parametrize("failure", ["inspect", "disappeared"])
def test_dependency_release_cache_strict_read_does_not_treat_io_failures_as_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    key = _key()
    path = put_ai_result(key, {"x": 1}, programs_root=tmp_path)
    before = path.read_bytes()
    original_lstat, original_read = Path.lstat, Path.read_text

    def failing_lstat(self: Path, *args: object, **kwargs: object) -> object:
        if self == path and failure == "inspect":
            raise PermissionError("synthetic-secret-path")
        return original_lstat(self, *args, **kwargs)

    def failing_read(self: Path, *args: object, **kwargs: object) -> str:
        if self == path and failure == "disappeared":
            raise FileNotFoundError("synthetic-secret-path")
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", failing_lstat)
    monkeypatch.setattr(Path, "read_text", failing_read)
    with pytest.raises(StateError) as raised:
        get_ai_result(key, programs_root=tmp_path, strict_read=True)
    assert "synthetic-secret-path" not in str(raised.value)
    assert path.read_bytes() == before


def test_dependency_release_cache_strict_read_rejects_nonregular_entry(tmp_path: Path) -> None:
    key = _key()
    path = put_ai_result(key, {"x": 1}, programs_root=tmp_path)
    path.unlink()
    path.mkdir()
    with pytest.raises(StateError, match="regular"):
        get_ai_result(key, programs_root=tmp_path, strict_read=True)
    assert path.is_dir()
