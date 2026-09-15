from __future__ import annotations

from knowledge_runtime.v2.contracts import ParseReport
from knowledge_runtime.v2.quality import QualityGate


def test_quality_gate_retries_low_table_quality() -> None:
    report = ParseReport(
        document_type="pdf",
        page_count=1,
        text_coverage=0.9,
        layout_quality=0.9,
        ocr_quality=0.9,
        table_quality=0.2,
        reading_order_quality=0.8,
        missing_regions=(),
        suspicious_regions=(),
        parser_name="local",
        parser_version="1",
        overall_grade="degraded",
        warnings=(),
    )
    assert QualityGate().evaluate(report).action == "RETRY"


def test_quality_gate_escalates_failed_parse() -> None:
    report = ParseReport(
        document_type="pdf",
        page_count=1,
        text_coverage=0.0,
        layout_quality=0.0,
        ocr_quality=0.0,
        table_quality=0.0,
        reading_order_quality=0.0,
        missing_regions=("page:1",),
        suspicious_regions=(),
        parser_name="local",
        parser_version="1",
        overall_grade="failed",
        warnings=("parser unavailable",),
    )
    decision = QualityGate().evaluate(report)
    assert decision.action == "ESCALATED"
    assert decision.reason == "parser_failed"


def test_quality_gate_accepts_high_quality_report() -> None:
    report = ParseReport.empty("docling", "2")
    assert QualityGate().evaluate(report).action == "ACCEPTED"

