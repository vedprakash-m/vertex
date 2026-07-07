from __future__ import annotations

from dataclasses import dataclass

from src.commands import decisions, gather, report_ai
from src.commands.gather_pipeline import ado_signal_builder_stage
from src.core import action_mapper, contradiction_engine, dependency_scout, workstream_path_resolver


@dataclass(frozen=True, slots=True)
class _WorkstreamStub:
    id: str
    area_paths: tuple[str, ...]


def test_resolve_workstream_id_loose_longest_prefers_longest_prefix() -> None:
    workstreams = (
        _WorkstreamStub(id="broad", area_paths=("One\\Adventure",)),
        _WorkstreamStub(id="specific", area_paths=("One\\Adventure\\Acme",)),
    )

    assert (
        workstream_path_resolver.resolve_workstream_id_loose_longest(
            "One\\Adventure\\Acme\\Checkout",
            workstreams,
        )
        == "specific"
    )


def test_resolve_workstream_id_loose_longest_keeps_non_separator_prefix_behavior() -> None:
    workstreams = (_WorkstreamStub(id="prefix", area_paths=("One\\Eng",)),)

    assert (
        workstream_path_resolver.resolve_workstream_id_loose_longest(
            "One\\Engineering\\Portal",
            workstreams,
        )
        == "prefix"
    )


def test_resolve_workstream_id_loose_longest_accepts_none() -> None:
    assert workstream_path_resolver.resolve_workstream_id_loose_longest(None, ()) is None


def test_resolve_workstream_id_strict_longest_prefers_longest_separator_match() -> None:
    workstreams = (
        _WorkstreamStub(id="broad", area_paths=("One\\Adventure",)),
        _WorkstreamStub(id="specific", area_paths=("One\\Adventure\\Acme",)),
    )

    assert (
        workstream_path_resolver.resolve_workstream_id_strict_longest(
            "One\\Adventure\\Acme\\Checkout",
            workstreams,
        )
        == "specific"
    )


def test_resolve_workstream_id_strict_longest_requires_separator_boundary() -> None:
    workstreams = (_WorkstreamStub(id="prefix", area_paths=("One\\Eng",)),)

    assert (
        workstream_path_resolver.resolve_workstream_id_strict_longest(
            "One\\Engineering\\Portal",
            workstreams,
        )
        is None
    )


def test_resolve_workstream_id_strict_longest_normalizes_trailing_slashes() -> None:
    workstreams = (_WorkstreamStub(id="acme", area_paths=("One\\Adventure\\Acme\\",)),)

    assert (
        workstream_path_resolver.resolve_workstream_id_strict_longest(
            "One\\Adventure\\Acme\\",
            workstreams,
        )
        == "acme"
    )


def test_modules_share_canonical_loose_resolver() -> None:
    assert gather._resolve_workstream_id is workstream_path_resolver.resolve_workstream_id_loose_longest
    assert report_ai._resolve_workstream_id is workstream_path_resolver.resolve_workstream_id_loose_longest
    assert contradiction_engine._resolve_workstream_id is workstream_path_resolver.resolve_workstream_id_loose_longest
    assert dependency_scout._resolve_workstream_id is workstream_path_resolver.resolve_workstream_id_loose_longest
    assert decisions._resolve_workstream_id is workstream_path_resolver.resolve_workstream_id_loose_longest


def test_modules_share_canonical_strict_resolver() -> None:
    assert (
        ado_signal_builder_stage._resolve_workstream_id
        is workstream_path_resolver.resolve_workstream_id_strict_longest
    )
    assert action_mapper._resolve_workstream_id is workstream_path_resolver.resolve_workstream_id_strict_longest
