from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .contracts import ParseReport


QualityAction = Literal["ACCEPTED", "RETRY", "ESCALATED"]


@dataclass(frozen=True)
class QualityDecision:
    action: QualityAction
    reason: str
    retry_provider: str | None = None


@dataclass(frozen=True)
class QualityGate:
    retry_threshold: float = 0.65

    def evaluate(self, report: ParseReport) -> QualityDecision:
        if report.overall_grade == "failed":
            return QualityDecision("ESCALATED", "parser_failed")
        scores = (
            report.text_coverage,
            report.layout_quality,
            report.ocr_quality,
            report.table_quality,
            report.reading_order_quality,
        )
        if min(scores) < self.retry_threshold:
            return QualityDecision("RETRY", "quality_below_threshold")
        return QualityDecision("ACCEPTED", "quality_passed")

