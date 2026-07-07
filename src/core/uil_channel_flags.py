from __future__ import annotations

import os
from typing import Callable


UIL_CHANNEL_ENV_FLAGS: dict[str, str] = {
    "ado": "VERTEX_UIL_ADO",
    "kusto": "VERTEX_UIL_KUSTO",
    "teams": "VERTEX_UIL_TEAMS",
    "icm": "VERTEX_UIL_ICM",
}


def _env_flag(name: str) -> bool:
    return os.getenv(name, "0").strip().lower() in {"1", "true", "yes", "on"}


def uil_channel_enabled(channel: str) -> bool:
    env_var = UIL_CHANNEL_ENV_FLAGS.get(channel)
    if env_var is None:
        return False
    return _env_flag(env_var)


def uil_ado_enabled() -> bool:
    return True


def uil_kusto_enabled() -> bool:
    return uil_channel_enabled("kusto")


def uil_teams_enabled() -> bool:
    return uil_channel_enabled("teams")


def uil_icm_enabled() -> bool:
    return uil_channel_enabled("icm")


UIL_CHANNEL_ENABLED_FUNCS: dict[str, Callable[[], bool]] = {
    "ado": uil_ado_enabled,
    "kusto": uil_kusto_enabled,
    "teams": uil_teams_enabled,
    "icm": uil_icm_enabled,
}
