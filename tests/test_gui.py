from __future__ import annotations

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
from aruba2930f_backup.gui import (
    BackupCallbacks,
    BackupOutcome,
    BackupRequest,
    CollectorBackupService,
    HostKeyApprovalDialog,
    MainWindow,
)
from aruba2930f_backup.hostkeys import HostKeyStore, sha256_fingerprint
from aruba2930f_backup.models import (
    CollectionOptions,
    DeviceResult,
    DeviceStatus,
    DeviceTarget,
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

    def probe_host_keys(
        self,
        targets: Sequence[DeviceTarget],
        *,
        options: CollectionOptions | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[HostKeyCheck]:
        del options, cancel_event
        return [
            HostKeyCheck(
                observation=HostKeyObservation(
                    target=target,
                    key_type="ssh-ed25519",
                    fingerprint="SHA256:fixture",
                ),
                state=HostKeyTrustState.TRUSTED,
            )
            for target in targets
        ]

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
    dialog.close()


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
        def probe_host_keys(
            self,
            targets: Sequence[DeviceTarget],
            *,
            options: CollectionOptions | None = None,
            cancel_event: threading.Event | None = None,
        ) -> list[HostKeyCheck]:
            del options, cancel_event
            return [
                HostKeyCheck(
                    observation=HostKeyObservation(targets[0], "", ""),
                    state=HostKeyTrustState.REJECTED,
                    message="The SSH endpoint timed out.",
                    error_code=ErrorCode.TCP_TIMEOUT,
                    attempts=4,
                ),
                HostKeyCheck(
                    observation=HostKeyObservation(
                        targets[1], "ssh-ed25519", "SHA256:trusted-fixture"
                    ),
                    state=HostKeyTrustState.TRUSTED,
                ),
            ]

    collector = PartiallyReachableCollector()
    outcome = CollectorBackupService(collector).run(
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
    assert first["status"] == DeviceStatus.FAILED
    assert first["error_code"] == ErrorCode.TCP_TIMEOUT
    assert first["attempts"] == 4
    assert second["target"]["ip"] == "192.0.2.11"
    assert second["status"] == DeviceStatus.SUCCESS


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

    assert run_once().results[0]["status"] is DeviceStatus.SUCCESS
    service.cancel()
    assert run_once().results[0]["status"] is DeviceStatus.SUCCESS


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
