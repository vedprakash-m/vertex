from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.commands.doctor_checks.actuation_checks import run_actuation_policy_check
from src.core.actuation_engine import ActuationPolicy


def test_run_actuation_policy_check_states(tmp_path: Path) -> None:
    rule_on = SimpleNamespace(id="r-on", enabled=True)
    rule_off = SimpleNamespace(id="r-off", enabled=False)

    disabled = ActuationPolicy(
        schema_version="1", enabled=False, approval_ttl_hours=24, per_adapter_per_run_cap=10, rules=(rule_off,)
    )
    with patch("src.core.actuation_engine.load_actuation_policy", return_value=disabled):
        res = run_actuation_policy_check("acme", programs_root=tmp_path)
        assert res.status == "ok"

    enabled_no_rules = ActuationPolicy(
        schema_version="1", enabled=True, approval_ttl_hours=24, per_adapter_per_run_cap=10, rules=(rule_off,)
    )
    with patch("src.core.actuation_engine.load_actuation_policy", return_value=enabled_no_rules):
        res = run_actuation_policy_check("acme", programs_root=tmp_path)
        assert res.status == "ok"

    enabled_active = ActuationPolicy(
        schema_version="1", enabled=True, approval_ttl_hours=24, per_adapter_per_run_cap=10, rules=(rule_on,)
    )
    with patch("src.core.actuation_engine.load_actuation_policy", return_value=enabled_active):
        res = run_actuation_policy_check("acme", programs_root=tmp_path)
        assert res.status == "warn"
        assert res.metadata == {"program_id": "acme", "enabled_rules": ["r-on"]}

    with patch("src.core.actuation_engine.load_actuation_policy", side_effect=ValueError("bad schema")):
        assert run_actuation_policy_check("acme", programs_root=tmp_path).status == "fail"

    with patch("src.core.actuation_engine.load_actuation_policy", side_effect=RuntimeError("boom")):
        assert run_actuation_policy_check("acme", programs_root=tmp_path).status == "warn"
