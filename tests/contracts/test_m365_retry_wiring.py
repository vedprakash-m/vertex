"""WS-17 contract tests: M365 retry wrappers.

The retry layer in ``src/m365/retry_subprocess.py`` and its wire-in at
``agency_bridge`` must:

- Retry on ``subprocess.TimeoutExpired`` (always — transient).
- Retry on caller-provided ``retryable_exit_codes`` + the platform defaults
  (1, 2, 75, 143).
- NOT retry on ``FileNotFoundError`` / ``PermissionError`` (fatal —
  retrying won't help).
- NOT retry on non-retryable exit codes (caller wants to see them).
- Stop at ``max_attempts`` (no unbounded retry).
- Be a no-op for callers that inject a custom ``runner`` (test
  infrastructure passes fake runners; the retry wrapper would
  otherwise mask the call counts the tests assert on).
"""
from __future__ import annotations

import subprocess
import unittest.mock as mock

import pytest

from src.m365.retry_subprocess import (
    build_subprocess_runner,
    retry_subprocess_call,
)


# ---------------------------------------------------------------------------
# ``retry_subprocess_call`` library tests
# ---------------------------------------------------------------------------


def _ok_result() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def test_returns_immediately_on_success() -> None:
    runner = mock.Mock(return_value=_ok_result())
    sleep = mock.Mock()
    result = retry_subprocess_call(runner, [], timeout=1, sleep_func=sleep)
    assert result.returncode == 0
    assert runner.call_count == 1
    assert not sleep.called


def test_retries_on_timeout_then_succeeds() -> None:
    runner = mock.Mock(
        side_effect=[
            subprocess.TimeoutExpired(cmd="x", timeout=1),
            subprocess.TimeoutExpired(cmd="x", timeout=1),
            _ok_result(),
        ]
    )
    sleep = mock.Mock()
    result = retry_subprocess_call(
        runner, [], timeout=1, max_attempts=3, base_delay=0.0,
        jitter_max=0.0, sleep_func=sleep,
    )
    assert result.returncode == 0
    assert runner.call_count == 3
    assert sleep.call_count == 2  # sleeps between the 3 attempts


def test_raises_last_timeout_after_max_attempts() -> None:
    runner = mock.Mock(
        side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1)
    )
    sleep = mock.Mock()
    with pytest.raises(subprocess.TimeoutExpired):
        retry_subprocess_call(
            runner, [], timeout=1, max_attempts=2, base_delay=0.0,
            jitter_max=0.0, sleep_func=sleep,
        )
    assert runner.call_count == 2


def test_does_not_retry_on_filenotfound() -> None:
    runner = mock.Mock(side_effect=FileNotFoundError("no workiq"))
    sleep = mock.Mock()
    with pytest.raises(FileNotFoundError):
        retry_subprocess_call(
            runner, [], timeout=1, max_attempts=5, sleep_func=sleep,
        )
    assert runner.call_count == 1
    assert not sleep.called


def test_does_not_retry_on_permissionerror() -> None:
    runner = mock.Mock(side_effect=PermissionError("not exec"))
    sleep = mock.Mock()
    with pytest.raises(PermissionError):
        retry_subprocess_call(
            runner, [], timeout=1, max_attempts=5, sleep_func=sleep,
        )
    assert runner.call_count == 1


def test_retries_on_retryable_exit_code_then_succeeds() -> None:
    fail = subprocess.CompletedProcess(args=[], returncode=75, stdout="", stderr="")
    runner = mock.Mock(side_effect=[fail, fail, _ok_result()])
    sleep = mock.Mock()
    result = retry_subprocess_call(
        runner, [], timeout=1, max_attempts=3, base_delay=0.0,
        jitter_max=0.0, sleep_func=sleep,
        retryable_exit_codes=(75,),
    )
    assert result.returncode == 0
    assert runner.call_count == 3


def test_does_not_retry_on_non_retryable_exit_code() -> None:
    fail = subprocess.CompletedProcess(args=[], returncode=99, stdout="", stderr="")
    runner = mock.Mock(return_value=fail)
    sleep = mock.Mock()
    result = retry_subprocess_call(
        runner, [], timeout=1, max_attempts=5, sleep_func=sleep,
    )
    assert result.returncode == 99
    assert runner.call_count == 1
    assert not sleep.called


def test_returns_final_failure_when_max_attempts_reached_on_retryable_exit() -> None:
    fail = subprocess.CompletedProcess(args=[], returncode=75, stdout="", stderr="")
    runner = mock.Mock(return_value=fail)
    sleep = mock.Mock()
    result = retry_subprocess_call(
        runner, [], timeout=1, max_attempts=3, base_delay=0.0,
        jitter_max=0.0, sleep_func=sleep, retryable_exit_codes=(75,),
    )
    assert result.returncode == 75
    assert runner.call_count == 3
    assert sleep.call_count == 2


def test_default_retryable_exit_codes_include_75_and_143() -> None:
    """The platform defaults (1, 2, 75, 143) must apply when the caller
    passes an empty retryable set — protects against operators that
    never customize the bridge."""
    fail = subprocess.CompletedProcess(args=[], returncode=143, stdout="", stderr="")
    runner = mock.Mock(side_effect=[fail, _ok_result()])
    sleep = mock.Mock()
    result = retry_subprocess_call(
        runner, [], timeout=1, max_attempts=2, base_delay=0.0,
        jitter_max=0.0, sleep_func=sleep,
    )
    assert result.returncode == 0
    assert runner.call_count == 2


def test_sleep_uses_exponential_curve() -> None:
    runner = mock.Mock(
        side_effect=[
            subprocess.TimeoutExpired(cmd="x", timeout=1),
            subprocess.TimeoutExpired(cmd="x", timeout=1),
            _ok_result(),
        ]
    )
    sleep = mock.Mock()
    retry_subprocess_call(
        runner, [], timeout=1, max_attempts=3,
        base_delay=1.0, max_delay=10.0, jitter_max=0.0, sleep_func=sleep,
    )
    # 1.0s then 2.0s (base * 2)
    assert sleep.call_count == 2
    assert sleep.call_args_list[0].args[0] == 1.0
    assert sleep.call_args_list[1].args[0] == 2.0


def test_sleep_respects_max_delay() -> None:
    runner = mock.Mock(
        side_effect=[
            subprocess.TimeoutExpired(cmd="x", timeout=1),
            subprocess.TimeoutExpired(cmd="x", timeout=1),
            subprocess.TimeoutExpired(cmd="x", timeout=1),
            _ok_result(),
        ]
    )
    sleep = mock.Mock()
    retry_subprocess_call(
        runner, [], timeout=1, max_attempts=4,
        base_delay=1.0, max_delay=2.0, jitter_max=0.0, sleep_func=sleep,
    )
    # 1.0, 2.0 (cap), 2.0 (cap)
    delays = [c.args[0] for c in sleep.call_args_list]
    assert delays == [1.0, 2.0, 2.0]


# ---------------------------------------------------------------------------
# ``build_subprocess_runner`` factory tests
# ---------------------------------------------------------------------------


def test_build_returns_drop_in_runner() -> None:
    base = mock.Mock(return_value=_ok_result())
    wrapped = build_subprocess_runner(base, max_attempts=2, base_delay=0.0, jitter_max=0.0)
    # The wrapped runner is drop-in: same positional/keyword signature, returns
    # the result. ``sleep_func`` is a retry-internal concern; the wrapper
    # picks its own default so the caller does not need to pass one.
    result = wrapped(["echo", "hi"], timeout=1)
    assert result.returncode == 0
    assert base.call_count == 1
    base.assert_called_once_with(["echo", "hi"], timeout=1)


def test_wrapped_runner_retries_on_retryable_exit() -> None:
    base = mock.Mock(side_effect=[
        subprocess.CompletedProcess(args=[], returncode=75, stdout="", stderr=""),
        _ok_result(),
    ])
    wrapped = build_subprocess_runner(
        base, max_attempts=2, base_delay=0.0, jitter_max=0.0,
        retryable_exit_codes=(75,),
    )
    result = wrapped([], timeout=1)
    assert result.returncode == 0
    assert base.call_count == 2


# ---------------------------------------------------------------------------
# Source-level: ``agency_bridge`` default-runnner is retry-wrapped
# ---------------------------------------------------------------------------


def test_agency_bridge_default_runner_is_retry_wrapped() -> None:
    """The bridge's default ``runner`` (the one used when no
    test-injected ``runner=`` is passed) must be the retry-wrapped
    subprocess.run, not bare ``subprocess.run``. This is the WS-17
    wire-in for the agency_bridge path."""
    import ast
    from pathlib import Path
    bridge_path = Path(__file__).resolve().parents[2] / "src" / "m365" / "agency_bridge.py"
    text = bridge_path.read_text(encoding="utf-8")
    # Symbol must be imported
    assert "build_subprocess_runner" in text
    # And used in the __init__ — find the __init__ body.
    tree = ast.parse(text, filename=str(bridge_path))
    found_init = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "__init__":
            init_src = ast.unparse(node)
            assert "build_subprocess_runner" in init_src, (
                "agency_bridge.__init__ must wrap subprocess.run in "
                "build_subprocess_runner (WS-17 retry wire-in)"
            )
            found_init = True
    assert found_init, "agency_bridge.__init__ not found"
