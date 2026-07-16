"""ADF-W2.7: re-export shim. The real implementation moved to
``src/core/injection_detector.py`` (Zone A) -- pure regex/stdlib heuristics,
no provider SDK -- the same rationale ADF-W0.14 applied to token estimation.
Kept here unchanged so existing importers do not need to move.
"""

from __future__ import annotations

from src.core.injection_detector import InjectionDetector, InjectionSignal, ScanResult

__all__ = ["InjectionDetector", "InjectionSignal", "ScanResult"]
