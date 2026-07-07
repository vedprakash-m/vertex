from __future__ import annotations

from datetime import datetime, timezone

from src.core.ado_semantics import _vertex_service_identities, is_vertex_generated_comment
from src.core.models import Comment


def test_vertex_service_identities_reads_vertex_envs(monkeypatch) -> None:
    monkeypatch.setenv("VERTEX_SERVICE_IDENTITY", "vertex-bot@example.com")
    monkeypatch.setenv("VERTEX_SERVICE_IDENTITIES", "vertex-bot-2@example.com; vertex-bot-3@example.com")

    assert _vertex_service_identities() == {
        "vertex-bot@example.com",
        "vertex-bot-2@example.com",
        "vertex-bot-3@example.com",
    }


def test_is_vertex_generated_comment_matches_service_identity_alias(monkeypatch) -> None:
    monkeypatch.setenv("VERTEX_SERVICE_IDENTITY", "vertex-bot@example.com")
    comment = Comment(
        work_item_id=401,
        comment_id=1,
        created_by="Vertex Bot",
        created_by_email="vertex-bot@example.com",
        created_date=datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
        text="Manual note from service identity",
    )

    assert is_vertex_generated_comment(comment) is True