from __future__ import annotations

from src.core.signal_fragment_utils import fragment_resource_id, split_signal_fragments


def test_split_signal_fragments_splits_multiline_work_item_updates() -> None:
    fragments = split_signal_fragments(
        "Bug 12345 remains blocked on SCHIE.\nTask 67890 mitigation owner confirmed."
    )

    assert fragments == (
        "Bug 12345 remains blocked on SCHIE.",
        "Task 67890 mitigation owner confirmed.",
    )


def test_split_signal_fragments_splits_sentence_level_multi_ref_updates() -> None:
    fragments = split_signal_fragments(
        "WI:12345 remains blocked. WI:67890 owner confirmed mitigation."
    )

    assert fragments == (
        "WI:12345 remains blocked.",
        "WI:67890 owner confirmed mitigation.",
    )


def test_fragment_resource_id_is_stable_for_segmented_resources() -> None:
    assert fragment_resource_id(resource_id="msg-123", segment_index=0, segment_count=2) == "msg-123:seg:0"
    assert fragment_resource_id(resource_id="msg-123", segment_index=0, segment_count=1) == "msg-123"
