from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from openpyxl import load_workbook

from aruba2930f_backup.reporting import (
    ReportSummary,
    SanitizedJsonlLogger,
    sanitize_log_event,
    write_result_workbook,
)


def test_result_workbook_has_expected_sheets_fields_and_formula_protection(tmp_path: Path) -> None:
    started = datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC)
    result = {
        "target": {"ip": "192.0.2.10"},
        "hostname": '=HYPERLINK("https://invalid.example")',
        "model": "Aruba 2930F",
        "sku": "JL253A",
        "status": "success",
        "attempts": 1,
        "started_at": started,
        "finished_at": started + timedelta(seconds=2),
        "config_path": tmp_path / "edge.txt",
        "config_sha256": "a" * 64,
        "error_code": "",
        "error_message": "+untrusted formula text",
    }

    report_path = write_result_workbook(
        tmp_path,
        [result],
        summary=ReportSummary(
            started_at=started,
            finished_at=started + timedelta(seconds=2),
        ),
    )

    workbook = load_workbook(report_path, data_only=False)
    assert workbook.sheetnames == ["Summary", "Devices"]
    devices = workbook["Devices"]
    assert [cell.value for cell in devices[1]] == [
        "IP",
        "Hostname",
        "Model/SKU",
        "Status",
        "Attempts",
        "Started At",
        "Finished At",
        "Duration Seconds",
        "Config File",
        "SHA-256",
        "Error Code",
        "Error Message",
    ]
    assert devices["A2"].value == "192.0.2.10"
    assert devices["B2"].value.startswith("'=")
    assert devices["L2"].value.startswith("'+")
    assert devices["D2"].value == "success"
    assert workbook["Summary"]["B5"].value == 1
    assert not (tmp_path / "result.xlsx.part").exists()
    workbook.close()


def test_jsonl_logger_allowlists_fields_and_redacts_sensitive_values(tmp_path: Path) -> None:
    path = tmp_path / "operation.jsonl"
    logger = SanitizedJsonlLogger(
        path,
        sensitive_values=("operator", "secret-password", "edge-switch"),
    )

    returned = logger.log(
        stage="connecting",
        status="failed",
        error_code="TCP_TIMEOUT",
        message="operator at 192.0.2.10 edge-switch secret-password\nnext line",
        ip="192.0.2.10",
        password="secret-password",
        raw_output="running configuration",
    )

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record == returned
    encoded = json.dumps(record)
    assert "192.0.2.10" not in encoded
    assert "secret-password" not in encoded
    assert "operator" not in encoded
    assert "edge-switch" not in encoded
    assert "raw_output" not in record
    assert "password" not in record
    assert record["message"].count("<redacted") >= 4


def test_sanitize_log_event_does_not_serialize_arbitrary_object_fields() -> None:
    event = {
        "stage": "reading_config",
        "attempt": 1,
        "config_text": "password manager plaintext",
        "target": {"ip": "198.51.100.2"},
    }

    record = sanitize_log_event(event)

    assert record["stage"] == "reading_config"
    assert record["attempt"] == 1
    assert "config_text" not in record
    assert "target" not in record
