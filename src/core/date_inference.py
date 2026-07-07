from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from src.core.models import Confidence


@dataclass(frozen=True, slots=True)
class DateInferenceResult:
    inferred_date: date | None
    is_inferred: bool
    source: str | None
    confidence: Confidence

    @classmethod
    def from_explicit(cls, value: date | datetime) -> DateInferenceResult:
        return cls(
            inferred_date=_normalize_date(value),
            is_inferred=False,
            source=None,
            confidence=Confidence.HIGH,
        )

    @classmethod
    def from_iteration(cls, value: date, iteration_path: str) -> DateInferenceResult:
        return cls(
            inferred_date=value,
            is_inferred=True,
            source=f"IterationPath: {iteration_path}",
            confidence=Confidence.MEDIUM,
        )

    @classmethod
    def from_semester(cls, value: date, semester: str) -> DateInferenceResult:
        return cls(
            inferred_date=value,
            is_inferred=True,
            source=f"Semester boundary: {semester}",
            confidence=Confidence.LOW,
        )

    @classmethod
    def no_date(cls) -> DateInferenceResult:
        return cls(
            inferred_date=None,
            is_inferred=False,
            source=None,
            confidence=Confidence.NONE,
        )


def _normalize_date(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value


class DateInference:
    QUARTER_END_DATES = {
        "Q1": (9, 30),
        "Q2": (12, 31),
        "Q3": (3, 31),
        "Q4": (6, 30),
    }

    MONTH_DAYS = {
        "january": (1, 31),
        "february": (2, 28),
        "march": (3, 31),
        "april": (4, 30),
        "may": (5, 31),
        "june": (6, 30),
        "july": (7, 31),
        "august": (8, 31),
        "september": (9, 30),
        "october": (10, 31),
        "november": (11, 30),
        "december": (12, 31),
        "jan": (1, 31),
        "feb": (2, 28),
        "mar": (3, 31),
        "apr": (4, 30),
        "jun": (6, 30),
        "jul": (7, 31),
        "aug": (8, 31),
        "sep": (9, 30),
        "oct": (10, 31),
        "nov": (11, 30),
        "dec": (12, 31),
    }

    SEMESTER_END_DATES = {
        "H1": (12, 31),
        "H2": (6, 30),
        "S1": (12, 31),
        "S2": (6, 30),
    }

    @classmethod
    def infer_date(
        cls,
        target_date: date | datetime | None,
        iteration_path: str | None,
        current_semester: str | None = None,
    ) -> DateInferenceResult:
        if target_date is not None:
            return DateInferenceResult.from_explicit(target_date)

        if iteration_path:
            result = cls._infer_from_iteration_path(iteration_path)
            if result.inferred_date is not None:
                return result

        if current_semester:
            result = cls._infer_from_semester(current_semester)
            if result.inferred_date is not None:
                return result

        return DateInferenceResult.no_date()

    @classmethod
    def _infer_from_iteration_path(cls, iteration_path: str) -> DateInferenceResult:
        if not iteration_path:
            return DateInferenceResult.no_date()

        parts = [part.strip() for part in iteration_path.replace("/", "\\").split("\\") if part.strip()]
        fiscal_year = cls._extract_fiscal_year(parts)
        quarter = cls._extract_quarter(parts)
        month = cls._extract_month(parts)

        if month and fiscal_year:
            month_num, last_day = cls.MONTH_DAYS.get(month.lower(), (None, None))
            if month_num is not None and last_day is not None:
                year = cls._fy_to_calendar_year(fiscal_year, month_num)
                if month_num == 2 and cls._is_leap_year(year):
                    last_day = 29
                return DateInferenceResult.from_iteration(
                    date(year, month_num, last_day),
                    iteration_path,
                )

        if quarter and fiscal_year:
            month_num, day = cls.QUARTER_END_DATES.get(quarter, (None, None))
            if month_num is not None and day is not None:
                year = cls._fy_to_calendar_year(fiscal_year, month_num)
                return DateInferenceResult.from_iteration(
                    date(year, month_num, day),
                    iteration_path,
                )

        return DateInferenceResult.no_date()

    @classmethod
    def _infer_from_semester(cls, semester: str) -> DateInferenceResult:
        if not semester:
            return DateInferenceResult.no_date()

        semester_match = re.search(r"(H[12]|S[12])", semester.upper())
        if not semester_match:
            return DateInferenceResult.no_date()

        fy_match = re.search(r"FY(\d{2,4})", semester.upper())
        if not fy_match:
            return DateInferenceResult.no_date()

        fiscal_year = int(fy_match.group(1))
        if fiscal_year < 100:
            fiscal_year = 2000 + fiscal_year

        month_num, day = cls.SEMESTER_END_DATES.get(semester_match.group(1), (None, None))
        if month_num is None or day is None:
            return DateInferenceResult.no_date()

        year = cls._fy_to_calendar_year(fiscal_year, month_num)
        return DateInferenceResult.from_semester(date(year, month_num, day), semester)

    @classmethod
    def _extract_fiscal_year(cls, parts: list[str]) -> int | None:
        for part in parts:
            fiscal_year_match = re.search(r"FY\s*(\d{2,4})", part.upper())
            if fiscal_year_match:
                fiscal_year = int(fiscal_year_match.group(1))
                if fiscal_year < 100:
                    fiscal_year = 2000 + fiscal_year
                return fiscal_year

            year_match = re.fullmatch(r"20\d{2}", part)
            if year_match:
                return int(year_match.group(0))

        return None

    @classmethod
    def _extract_quarter(cls, parts: list[str]) -> str | None:
        for part in parts:
            quarter_match = re.search(r"(Q[1-4])", part.upper())
            if quarter_match:
                return quarter_match.group(1)
        return None

    @classmethod
    def _extract_month(cls, parts: list[str]) -> str | None:
        for part in parts:
            normalized = part.lower()
            if normalized in cls.MONTH_DAYS:
                return normalized
        return None

    @classmethod
    def _fy_to_calendar_year(cls, fiscal_year: int, month: int) -> int:
        if month >= 7:
            return fiscal_year - 1
        return fiscal_year

    @classmethod
    def _is_leap_year(cls, year: int) -> bool:
        return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def infer_target_date(
    target_date: date | datetime | None,
    iteration_path: str | None,
    current_semester: str | None = None,
) -> tuple[date | None, bool, str | None]:
    result = DateInference.infer_date(target_date, iteration_path, current_semester)
    return result.inferred_date, result.is_inferred, result.source
