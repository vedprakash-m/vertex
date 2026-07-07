from __future__ import annotations

from datetime import datetime, timezone
import unittest

from src.core.query_builder import build_odata_filter


class QueryBuilderTests(unittest.TestCase):
    def test_build_odata_filter_matches_expected_contract_shape(self) -> None:
        since = datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
        actual = build_odata_filter(
            area_paths=["One\\Adventure\\Acme", "One\\Adventure\\Contoso"],
            work_item_types=["Feature", "Risk"],
            since=since,
            states_excluded=["Removed", "Cut"],
        )
        expected = (
            "( startswith(Area/AreaPath, 'One\\Adventure\\Acme') or startswith(Area/AreaPath, 'One\\Adventure\\Contoso') ) "
            "and ( WorkItemType eq 'Feature' or WorkItemType eq 'Risk' ) "
            "and ChangedDate ge 2026-05-01T12:30:00Z "
            "and not ( State eq 'Removed' or State eq 'Cut' )"
        )
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()