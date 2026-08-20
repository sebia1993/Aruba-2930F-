from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication
from tests.fakes import ScriptedFactory

from aruba2930f_backup.collector import ArubaCollector
from aruba2930f_backup.diagnostics import decode_diagnostic_code, diagnostic_code_for_exception
from aruba2930f_backup.gui import (
    BackupCallbacks,
    BackupOutcome,
    BackupRequest,
    CollectorBackupService,
    DiagnosticCodesDialog,
    HostKeyApprovalDialog,
    MainWindow,
    TrustedKeysDialog,
)
from aruba2930f_backup.hostkeys import HostKeyStore, sha256_fingerprint
from aruba2930f_backup.models import (
    CollectionFailure,
    CollectionOptions,
    DeviceResult,
    DeviceStatus,
    DeviceTarget,
    DiagnosticDetail,
    DiagnosticPhase,
    ErrorCode,
    HostKeyCheck,
    HostKeyObservation,
    HostKeyTrustState,
)


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


class FakeService:
    def __init__(self, run_directory: Path) -> None:
        self.run_directory = run_directory
        self.request: BackupRequest | None = None
        self.cancelled = False

    def run(self, request: BackupRequest, callbacks: BackupCallbacks) -> BackupOutcome:
        self.request = request
        callbacks.on_event(
            {
                "target": {"ip": request.targets[0]},
                "stage": "completed",
                "attempt": 1,
            }
        )
        result = {
            "target": {"ip": request.targets[0]},
            "hostname": "lab-edge",
            "model": "Aruba 2930F",
            "sku": "JL253A",
            "status": "success",
            "host_key_attempts": 1,
            "attempts": 1,
        }
        return BackupOutcome(
            run_directory=self.run_directory,
            results=(result,),
            report_path=self.run_directory / "result.xlsx",
        )

    def cancel(self) -> None:
        self.cancelled = True


class FakeCollector:
    def __init__(self) -> None:
        self.cancelled = False
        self.begin_runs = 0
        self.result: DeviceResult | None = None

    def begin_run(self) -> None:
        self.begin_runs += 1
        self.cancelled = False

    def probe_host_keys_round(
        self,
        targets: Sequence[DeviceTarget],
        *,
        attempt: int,
        options: CollectionOptions | None = None,
        cancel_event: threading.Event | None = None,
        on_event: Callable[[object], None] | None = None,
    ) -> list[HostKeyCheck]:
        del options, cancel_event
        if on_event is not None:
            for target in targets:
                on_event(
                    {
                        "target": target,
                        "stage": "host_key_checking",
                        "round": attempt,
                        "attempt": attempt,
                    }
                )
        return [
            HostKeyCheck(
                observation=HostKeyObservation(
                    target=target,
                    key_type="ssh-ed25519",
                    fingerprint="SHA256:fixture",
                ),
                state=HostKeyTrustState.TRUSTED,
                attempts=attempt,
            )
            for target in targets
        ]

    def wait_for_retry_delay(
        self,
        delay_seconds: float,
        *,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        del delay_seconds
        return not bool(cancel_event and cancel_event.is_set())

    def approve_host_keys(self, checks: Iterable[HostKeyCheck]) -> None:
        raise AssertionError("Trusted fixture keys must not require approval")

    def collect_many(
        self,
        targets: Sequence[DeviceTarget],
        credentials: object,
        options: CollectionOptions | None = None,
        *,
        cancel_event: threading.Event | None = None,
        on_event: Callable[[object], None] | None = None,
    ) -> list[DeviceResult]:
        del credentials, options, cancel_event
        target = targets[0]
        if on_event is not None:
            on_event(
                {
                    "target": {"ip": target.ip},
                    "stage": "completed",
                    "attempt": 1,
                    "message": f"completed {target.ip}",
                }
            )
        now = datetime.now(UTC)
        self.result = DeviceResult(
            target=target,
            status=DeviceStatus.SUCCESS,
            attempts=1,
            started_at=now,
            finished_at=now,
            duration_seconds=0.1,
            hostname="fixture-edge",
            model="Aruba 2930F",
            sku="JL253A",
            config_text="hostname fixture-edge\nno telnet-server\n",
        )
        return [self.result]

    def cancel(self) -> None:
        self.cancelled = True


def _wait_until(app: QApplication, predicate: object, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if callable(predicate) and predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Qt condition was not reached before timeout")


@pytest.mark.gui
def test_main_window_defaults_and_request_are_session_only(
    app: QApplication, tmp_path: Path
) -> None:
    window = MainWindow(service=FakeService(tmp_path))
    window.ip_input.setPlainText("192.0.2.10\n198.51.100.8")
    window.username_input.setText("operator")
    window.password_input.setText("secret")
    window.enable_password_input.setText("enable-secret")
    window.output_input.setText(str(tmp_path))

    request = window.build_request()

    assert request.targets == ("192.0.2.10", "198.51.100.8")
    assert request.port == 22
    assert request.concurrency == 10
    assert request.username == "operator"
    assert request.password == "secret"
    assert request.enable_password == "enable-secret"
    window.close()


@pytest.mark.gui
def test_invalid_or_duplicate_ipv4_blocks_entire_request(app: QApplication) -> None:
    window = MainWindow(service=FakeService(Path.cwd()))

    with pytest.raises(ValueError, match="중복"):
        window.parse_targets("192.0.2.1\n192.0.2.1")
    with pytest.raises(ValueError, match="올바른 IPv4"):
        window.parse_targets("192.0.2.1\nnot-a-device")

    window.close()


@pytest.mark.gui
def test_changed_host_key_can_never_be_approved(app: QApplication) -> None:
    dialog = HostKeyApprovalDialog(
        [
            {
                "target": {"endpoint": "192.0.2.10:22"},
                "key_type": "ssh-ed25519",
                "fingerprint": "SHA256:example",
                "state": "changed",
            }
        ]
    )

    assert dialog.approval_allowed is False
    assert dialog.approve_button.isEnabled() is False
    assert dialog.windowTitle() == "SSH 장비 지문 확인"
    assert dialog.approve_button.text() == "표시된 지문 모두 승인"
    dialog.close()


@pytest.mark.gui
def test_host_key_controls_use_device_fingerprint_wording(app: QApplication) -> None:
    window = MainWindow(service=FakeService(Path.cwd()))
    dialog = TrustedKeysDialog((), None)

    assert window.trust_keys_button.text() == "SSH 장비 지문 관리…"
    assert dialog.windowTitle() == "SSH 장비 지문 관리"
    assert dialog.remove_button.text() == "선택 지문 제거"

    dialog.close()
    window.close()


@pytest.mark.gui
def test_injected_service_runs_off_ui_thread_and_updates_results(
    app: QApplication,
    tmp_path: Path,
) -> None:
    service = FakeService(tmp_path)
    window = MainWindow(service=service)
    window.ip_input.setPlainText("192.0.2.10")
    window.username_input.setText("operator")
    window.password_input.setText("secret")
    window.output_input.setText(str(tmp_path))

    window.start_button.click()
    _wait_until(app, lambda: window.start_button.isEnabled())

    assert service.request is not None
    assert window.progress_bar.value() == 100
    assert window.result_table.rowCount() == 1
    assert window.result_table.item(0, 1).text() == "lab-edge"
    assert window.password_input.text() == ""
    assert window.open_result_button.isEnabled()
    window.close()


@pytest.mark.gui
def test_retry_wait_status_does_not_advance_attempt_before_next_connection(
    app: QApplication,
) -> None:
    window = MainWindow(service=FakeService(Path.cwd()))
    window._target_count = 1
    target = {"ip": "192.0.2.20"}

    window._on_collection_event(
        {
            "target": target,
            "stage": "host_key_checking",
            "phase": "host_key",
            "round": 1,
            "attempt": 1,
        }
    )
    window._on_collection_event(
        {
            "target": target,
            "stage": "retry_wait",
            "phase": "host_key",
            "round": 2,
            "attempt": 2,
            "delay_seconds": 5.0,
        }
    )

    row = window._row_by_target["192.0.2.20"]
    assert window.result_table.item(row, 3).text() == "재시도 대기"
    assert window.result_table.item(row, 4).text() == "지문 1/4 · 백업 0/4"
    assert window.result_table.item(row, 3).foreground().color().name() == "#9a6700"
    window.close()


@pytest.mark.gui
def test_terminal_failed_event_counts_as_completed_progress(app: QApplication) -> None:
    window = MainWindow(service=FakeService(Path.cwd()))
    window._target_count = 1

    window._on_collection_event(
        {
            "target": {"ip": "192.0.2.21"},
            "stage": "failed",
            "phase": "backup",
            "round": 1,
            "attempt": 1,
            "error_code": "AUTH_FAILED",
        }
    )

    assert window.progress_bar.value() == 100
    assert "192.0.2.21" in window._completed_targets
    window.close()


def test_collector_service_writes_config_report_and_sanitized_log(tmp_path: Path) -> None:
    collector = FakeCollector()
    service = CollectorBackupService(collector)
    request = BackupRequest(
        targets=("192.0.2.10",),
        port=22,
        username="operator",
        password="secret-password",
        enable_password="enable-secret",
        concurrency=10,
        output_directory=tmp_path,
    )

    outcome = service.run(
        request,
        BackupCallbacks(
            on_event=lambda _event: None,
            request_host_key_approval=lambda _checks: False,
            cancel_event=threading.Event(),
        ),
    )

    assert collector.result is not None
    assert collector.begin_runs == 1
    assert collector.result.config_text is None
    assert collector.result.config_path is not None
    assert collector.result.config_path.read_bytes().endswith(b"\r\n")
    assert collector.result.config_sha256
    assert outcome.report_path.exists()
    log_text = (outcome.run_directory / "operation.jsonl").read_text(encoding="utf-8")
    assert "192.0.2.10" not in log_text
    assert "secret-password" not in log_text
    assert "enable-secret" not in log_text


def test_workbook_write_failure_has_report_diagnostic_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_workbook(*args: object, **kwargs: object) -> Path:
        del args, kwargs
        raise OSError("/sensitive/output/path")

    monkeypatch.setattr("aruba2930f_backup.gui.write_result_workbook", fail_workbook)
    service = CollectorBackupService(FakeCollector())

    with pytest.raises(CollectionFailure) as captured:
        service.run(
            BackupRequest(
                targets=("192.0.2.10",),
                port=22,
                username="operator",
                password="secret-password",
                enable_password=None,
                concurrency=1,
                output_directory=tmp_path,
            ),
            BackupCallbacks(
                on_event=lambda _event: None,
                request_host_key_approval=lambda _checks: False,
                cancel_event=threading.Event(),
            ),
        )

    assert captured.value.code is ErrorCode.REPORT_WRITE_FAILED
    assert captured.value.diagnostic_phase is DiagnosticPhase.REPORT_STORAGE
    log_path = next(tmp_path.rglob("operation.jsonl"))
    log_text = log_path.read_text(encoding="utf-8")
    assert "/sensitive/output/path" not in log_text
    fatal_record = next(
        json.loads(line)
        for line in log_text.splitlines()
        if json.loads(line).get("status") == "fatal_app"
    )
    assert fatal_record["diagnostic_code"]


def test_config_write_failure_updates_device_result_and_diagnostic_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_config(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("/sensitive/device-config/path")

    monkeypatch.setattr("aruba2930f_backup.gui.write_config_atomic", fail_config)
    outcome = CollectorBackupService(FakeCollector()).run(
        BackupRequest(
            targets=("192.0.2.10",),
            port=22,
            username="operator",
            password="secret-password",
            enable_password=None,
            concurrency=1,
            output_directory=tmp_path,
        ),
        BackupCallbacks(
            on_event=lambda _event: None,
            request_host_key_approval=lambda _checks: False,
            cancel_event=threading.Event(),
        ),
    )

    result = outcome.results[0]
    assert result["status"] is DeviceStatus.FAILED
    assert result["error_code"] is ErrorCode.REPORT_WRITE_FAILED
    assert result["failure_phase"] is DiagnosticPhase.REPORT_STORAGE
    assert result["diagnostic_detail"] is DiagnosticDetail.OS_ERROR
    code = str(result["diagnostic_code"])
    decoded = decode_diagnostic_code(code)
    assert decoded.phase is DiagnosticPhase.REPORT_STORAGE
    assert decoded.error_code is ErrorCode.REPORT_WRITE_FAILED
    assert "/sensitive/device-config/path" not in code
    log_text = (outcome.run_directory / "operation.jsonl").read_text(encoding="utf-8")
    assert "/sensitive/device-config/path" not in log_text
    assert code in log_text


def test_backup_request_repr_never_contains_secrets(tmp_path: Path) -> None:
    request = BackupRequest(
        targets=("192.0.2.10",),
        port=22,
        username="operator",
        password="secret-password",
        enable_password="enable-secret",
        concurrency=10,
        output_directory=tmp_path,
    )

    rendered = repr(request)
    assert "secret-password" not in rendered
    assert "enable-secret" not in rendered


def test_host_key_probe_failure_is_per_device_and_preserves_other_backup(tmp_path: Path) -> None:
    class PartiallyReachableCollector(FakeCollector):
        def __init__(self) -> None:
            super().__init__()
            self.call_log: list[str] = []

        def probe_host_keys_round(
            self,
            targets: Sequence[DeviceTarget],
            *,
            attempt: int,
            options: CollectionOptions | None = None,
            cancel_event: threading.Event | None = None,
            on_event: Callable[[object], None] | None = None,
        ) -> list[HostKeyCheck]:
            del options, cancel_event, on_event
            self.call_log.append(f"probe-{attempt}:" + ",".join(target.ip for target in targets))
            checks: list[HostKeyCheck] = []
            for target in targets:
                if target.ip == "192.0.2.10":
                    checks.append(
                        HostKeyCheck(
                            observation=HostKeyObservation(target, "", ""),
                            state=HostKeyTrustState.REJECTED,
                            message="The SSH endpoint timed out.",
                            error_code=ErrorCode.TCP_TIMEOUT,
                            attempts=attempt,
                            retryable=True,
                            retry_exhausted=attempt >= 4,
                        )
                    )
                else:
                    checks.append(
                        HostKeyCheck(
                            observation=HostKeyObservation(
                                target, "ssh-ed25519", "SHA256:trusted-fixture"
                            ),
                            state=HostKeyTrustState.TRUSTED,
                            attempts=attempt,
                        )
                    )
            return checks

        def collect_many(
            self,
            targets: Sequence[DeviceTarget],
            credentials: object,
            options: CollectionOptions | None = None,
            *,
            cancel_event: threading.Event | None = None,
            on_event: Callable[[object], None] | None = None,
        ) -> list[DeviceResult]:
            self.call_log.append("collect:" + ",".join(target.ip for target in targets))
            return super().collect_many(
                targets,
                credentials,
                options,
                cancel_event=cancel_event,
                on_event=on_event,
            )

    collector = PartiallyReachableCollector()
    outcome = CollectorBackupService(
        collector,
        retry_delays_seconds=(0.0, 0.0, 0.0),
    ).run(
        BackupRequest(
            targets=("192.0.2.10", "192.0.2.11"),
            port=22,
            username="operator",
            password="secret-password",
            enable_password=None,
            concurrency=10,
            output_directory=tmp_path,
        ),
        BackupCallbacks(
            on_event=lambda _event: None,
            request_host_key_approval=lambda _checks: False,
            cancel_event=threading.Event(),
        ),
    )

    first, second = outcome.results
    assert first["target"]["ip"] == "192.0.2.10"
    assert first["status"] == DeviceStatus.RETRY_EXHAUSTED
    assert first["error_code"] == ErrorCode.TCP_TIMEOUT
    assert first["host_key_attempts"] == 4
    assert first["attempts"] == 0
    assert first["diagnostic_code"]
    decoded = decode_diagnostic_code(str(first["diagnostic_code"]))
    assert decoded.phase is DiagnosticPhase.HOST_KEY
    assert decoded.host_key_attempts == 4
    assert decoded.backup_attempts == 0
    assert second["target"]["ip"] == "192.0.2.11"
    assert second["status"] == DeviceStatus.SUCCESS
    assert second["host_key_attempts"] == 1
    assert collector.call_log[:3] == [
        "probe-1:192.0.2.10,192.0.2.11",
        "collect:192.0.2.11",
        "probe-2:192.0.2.10",
    ]
    retry_log = next(
        record
        for record in (
            json.loads(line)
            for line in (outcome.run_directory / "operation.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        if record.get("stage") == "retry_wait"
    )
    assert retry_log["round"] == 2
    assert retry_log["attempt"] == 1
    assert retry_log["delay_seconds"] == 0.0
    assert retry_log["error_code"] == ErrorCode.TCP_TIMEOUT
    diagnostic_log = next(
        record
        for record in (
            json.loads(line)
            for line in (outcome.run_directory / "operation.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        if record.get("diagnostic_code") == first["diagnostic_code"]
    )
    assert diagnostic_log["count"] == 1


@pytest.mark.gui
def test_diagnostic_dialog_aggregates_and_copies_codes(
    app: QApplication,
    tmp_path: Path,
) -> None:
    window = MainWindow(service=FakeService(tmp_path))
    window.show()
    code = diagnostic_code_for_exception(RuntimeError("must not be shown"), version="0.1.3")

    window._pending_diagnostic_counts = {code: 2}
    window._finalize_completed_run()
    app.processEvents()

    dialog = window._diagnostic_dialog
    assert isinstance(dialog, DiagnosticCodesDialog)
    expected = f"{code} \N{MULTIPLICATION SIGN} 2"
    assert dialog.copy_text == expected
    dialog.copy_button.click()
    assert QApplication.clipboard().text() == expected
    dialog.close()
    window.close()


@pytest.mark.gui
def test_fatal_worker_code_remains_in_status_and_opens_dialog(
    app: QApplication,
    tmp_path: Path,
) -> None:
    window = MainWindow(service=FakeService(tmp_path))
    window.show()
    code = diagnostic_code_for_exception(RuntimeError("not serialized"), version="0.1.3")

    window._on_worker_failure({"exception_name": "RuntimeError", "diagnostic_code": code})
    window._finalize_completed_run()
    app.processEvents()

    assert code in window.status_label.text()
    assert isinstance(window._diagnostic_dialog, DiagnosticCodesDialog)
    window._diagnostic_dialog.close()
    window.close()


@pytest.mark.gui
def test_retry_exhausted_button_runs_only_captured_subset_with_new_password(
    app: QApplication,
    tmp_path: Path,
) -> None:
    class RetrySubsetService:
        def __init__(self) -> None:
            self.requests: list[BackupRequest] = []

        def run(self, request: BackupRequest, callbacks: BackupCallbacks) -> BackupOutcome:
            del callbacks
            self.requests.append(request)
            run_number = len(self.requests)
            run_directory = tmp_path / f"run-{run_number}"
            run_directory.mkdir()
            results: list[dict[str, object]] = []
            for target in request.targets:
                exhausted = target == "192.0.2.11"
                results.append(
                    {
                        "target": {"ip": target},
                        "status": "retry_exhausted" if exhausted else "success",
                        "host_key_attempts": 1,
                        "attempts": 4 if exhausted else 1,
                        "error_code": "TCP_TIMEOUT" if exhausted else "",
                    }
                )
            return BackupOutcome(
                run_directory=run_directory,
                results=tuple(results),
                report_path=run_directory / "result.xlsx",
            )

        def cancel(self) -> None:
            pass

    service = RetrySubsetService()
    window = MainWindow(service=service)
    window.ip_input.setPlainText("192.0.2.10\n192.0.2.11")
    window.username_input.setText("operator")
    window.password_input.setText("first-password")
    window.output_input.setText(str(tmp_path))

    window.start_button.click()
    _wait_until(app, lambda: window.start_button.isEnabled())

    assert window.retry_exhausted_button.isEnabled()
    exhausted_row = window._row_by_target["192.0.2.11"]
    assert window.result_table.item(exhausted_row, 3).text() == "재시도 소진"
    assert window.result_table.item(exhausted_row, 4).text() == "지문 1/4 · 백업 4/4"
    assert window.password_input.text() == ""
    assert window.ip_input.toPlainText() == "192.0.2.10\n192.0.2.11"

    window.port_input.setValue(2222)
    window.username_input.setText("retry-operator")
    window.concurrency_input.setValue(3)
    window.password_input.setText("new-password")
    window.retry_exhausted_button.click()
    _wait_until(app, lambda: len(service.requests) == 2 and window.start_button.isEnabled())

    assert service.requests[1].targets == ("192.0.2.11",)
    assert service.requests[1].port == 2222
    assert service.requests[1].username == "retry-operator"
    assert service.requests[1].concurrency == 3
    assert service.requests[1].password == "new-password"
    assert window.result_table.rowCount() == 1
    assert service.requests[0].output_directory == service.requests[1].output_directory
    assert (tmp_path / "run-1") != (tmp_path / "run-2")
    window.close()


def test_same_service_runs_successfully_after_prior_cancel(tmp_path: Path) -> None:
    target = DeviceTarget("192.0.2.50")
    observation = HostKeyObservation(
        target,
        "ssh-ed25519",
        sha256_fingerprint(b"reusable-fixture-key"),
    )

    class Probe:
        def probe(self, requested: DeviceTarget, *, timeout: float = 15.0) -> HostKeyObservation:
            del timeout
            assert requested == target
            return observation

    collector = ArubaCollector(
        ScriptedFactory(),
        host_key_store=HostKeyStore(tmp_path / "known_hosts.json"),
        host_key_probe=Probe(),
    )
    service = CollectorBackupService(collector)
    request = BackupRequest(
        targets=(target.ip,),
        port=target.port,
        username="operator",
        password="test-password",
        enable_password=None,
        concurrency=1,
        output_directory=tmp_path / "backups",
    )

    def run_once() -> BackupOutcome:
        return service.run(
            request,
            BackupCallbacks(
                on_event=lambda _event: None,
                request_host_key_approval=lambda _checks: True,
                cancel_event=threading.Event(),
            ),
        )

    first_outcome = run_once()
    assert first_outcome.results[0]["status"] is DeviceStatus.SUCCESS
    service.cancel()
    second_outcome = run_once()
    assert second_outcome.results[0]["status"] is DeviceStatus.SUCCESS
    assert first_outcome.run_directory != second_outcome.run_directory
    assert first_outcome.report_path.exists()
    assert second_outcome.report_path.exists()


def test_cancel_during_preflight_retry_wait_writes_cancelled_report(tmp_path: Path) -> None:
    class CancelDuringWaitCollector(FakeCollector):
        def probe_host_keys_round(
            self,
            targets: Sequence[DeviceTarget],
            *,
            attempt: int,
            options: CollectionOptions | None = None,
            cancel_event: threading.Event | None = None,
            on_event: Callable[[object], None] | None = None,
        ) -> list[HostKeyCheck]:
            del options, cancel_event, on_event
            assert attempt == 1
            return [
                HostKeyCheck(
                    observation=HostKeyObservation(target, "", ""),
                    state=HostKeyTrustState.REJECTED,
                    message="The SSH endpoint timed out.",
                    error_code=ErrorCode.TCP_TIMEOUT,
                    attempts=attempt,
                    retryable=True,
                )
                for target in targets
            ]

        def wait_for_retry_delay(
            self,
            delay_seconds: float,
            *,
            cancel_event: threading.Event | None = None,
        ) -> bool:
            assert delay_seconds >= 0
            assert cancel_event is not None
            cancel_event.set()
            return False

    events: list[object] = []
    outcome = CollectorBackupService(CancelDuringWaitCollector()).run(
        BackupRequest(
            targets=("192.0.2.75",),
            port=22,
            username="operator",
            password="test-password",
            enable_password=None,
            concurrency=1,
            output_directory=tmp_path,
        ),
        BackupCallbacks(
            on_event=events.append,
            request_host_key_approval=lambda _checks: False,
            cancel_event=threading.Event(),
        ),
    )

    assert outcome.cancelled is True
    assert outcome.report_path.exists()
    assert outcome.results[0]["status"] is DeviceStatus.CANCELLED
    assert outcome.results[0]["host_key_attempts"] == 1
    assert outcome.results[0]["attempts"] == 0
    assert any(
        str(getattr(event.get("stage"), "value", event.get("stage"))) == "retry_wait"
        for event in cast(list[dict[str, object]], events)
    )


def test_cancel_mid_host_key_round_preserves_only_started_attempts(tmp_path: Path) -> None:
    class CancelMidRoundCollector(FakeCollector):
        def probe_host_keys_round(
            self,
            targets: Sequence[DeviceTarget],
            *,
            attempt: int,
            options: CollectionOptions | None = None,
            cancel_event: threading.Event | None = None,
            on_event: Callable[[object], None] | None = None,
        ) -> list[HostKeyCheck]:
            del options
            assert attempt == 1
            assert cancel_event is not None
            assert on_event is not None
            for target in targets[:2]:
                on_event(
                    {
                        "target": target,
                        "stage": "host_key_checking",
                        "attempt": attempt,
                    }
                )
            cancel_event.set()
            raise CollectionFailure(
                ErrorCode.CANCELLED,
                "Host-key review was cancelled.",
            )

    outcome = CollectorBackupService(CancelMidRoundCollector()).run(
        BackupRequest(
            targets=("192.0.2.75", "192.0.2.76", "192.0.2.77"),
            port=22,
            username="operator",
            password="test-password",
            enable_password=None,
            concurrency=2,
            output_directory=tmp_path,
        ),
        BackupCallbacks(
            on_event=lambda _event: None,
            request_host_key_approval=lambda _checks: False,
            cancel_event=threading.Event(),
        ),
    )

    assert outcome.cancelled is True
    assert [result["status"] for result in outcome.results] == [
        DeviceStatus.CANCELLED,
        DeviceStatus.CANCELLED,
        DeviceStatus.CANCELLED,
    ]
    assert [result["host_key_attempts"] for result in outcome.results] == [1, 1, 0]
    assert [result["attempts"] for result in outcome.results] == [0, 0, 0]


@pytest.mark.gui
def test_cancel_handler_does_not_block_on_slow_transport_close(app: QApplication) -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class SlowCancelService:
        def run(self, request: BackupRequest, callbacks: BackupCallbacks) -> BackupOutcome:
            del request, callbacks
            raise AssertionError("run is not used by this cancellation test")

        def cancel(self) -> None:
            entered.set()
            try:
                release.wait(timeout=2)
            finally:
                finished.set()

    class ThreadSentinel:
        def deleteLater(self) -> None:
            pass

    window = MainWindow(service=SlowCancelService())
    window.show()
    window._set_running(True)
    window._thread = cast(QThread, ThreadSentinel())
    window._cancel_event = threading.Event()

    started = time.monotonic()
    window._cancel_backup()
    elapsed = time.monotonic() - started

    assert elapsed < 0.1
    assert window._cancel_event.is_set()
    assert entered.wait(timeout=1)
    cancel_thread = window._cancel_thread
    assert cancel_thread is not None
    window._on_thread_finished()
    assert not window.start_button.isEnabled()
    window.close()
    app.processEvents()
    assert window.isVisible()

    release.set()
    assert finished.wait(timeout=1)
    deadline = time.monotonic() + 1
    while window.isVisible() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    cancel_thread.join(timeout=1)
    assert not cancel_thread.is_alive()
    assert not window.isVisible()
