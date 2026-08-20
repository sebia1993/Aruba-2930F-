"""Native-feeling PySide6 operator interface."""

from __future__ import annotations

import ipaddress
import sys
import threading
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, cast

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .models import (
    CollectionFailure,
    CollectionOptions,
    Credentials,
    DeviceResult,
    DeviceStatus,
    DeviceTarget,
    ErrorCode,
    HostKeyCheck,
    HostKeyTrustState,
)
from .reporting import ReportSummary, SanitizedJsonlLogger, write_result_workbook
from .storage import (
    create_run_directory,
    default_output_directory,
    device_config_path,
    write_config_atomic,
)


def _value_at(value: object, path: str, default: Any = "") -> Any:
    current: Any = value
    for part in path.split("."):
        current = current.get(part) if isinstance(current, dict) else getattr(current, part, None)
        if current is None:
            return default
    if isinstance(current, Enum):
        return current.value
    return current


def _first_value(value: object, *paths: str, default: Any = "") -> Any:
    for path in paths:
        candidate = _value_at(value, path, None)
        if candidate is not None:
            return candidate
    return default


@dataclass(frozen=True, slots=True)
class BackupRequest:
    """Session-only values passed from the form to the backup service."""

    targets: tuple[str, ...]
    port: int
    username: str
    password: str = field(repr=False)
    enable_password: str | None = field(repr=False)
    concurrency: int
    output_directory: Path


@dataclass(frozen=True, slots=True)
class BackupOutcome:
    run_directory: Path
    results: tuple[object, ...]
    report_path: Path
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class BackupCallbacks:
    """Thread-safe callbacks supplied to an injectable backup service."""

    on_event: Callable[[object], None]
    request_host_key_approval: Callable[[Sequence[object]], bool]
    cancel_event: threading.Event

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()


class BackupServiceProtocol(Protocol):
    def run(self, request: BackupRequest, callbacks: BackupCallbacks) -> BackupOutcome: ...

    def cancel(self) -> None: ...


class CollectorProtocol(Protocol):
    def begin_run(self) -> None: ...

    def probe_host_keys(
        self,
        targets: Sequence[DeviceTarget],
        *,
        options: CollectionOptions | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[HostKeyCheck]: ...

    def approve_host_keys(self, checks: Sequence[HostKeyCheck]) -> None: ...

    def collect_many(
        self,
        targets: Sequence[DeviceTarget],
        credentials: Credentials,
        options: CollectionOptions | None = None,
        *,
        cancel_event: threading.Event | None = None,
        on_event: Callable[[object], None] | None = None,
    ) -> list[DeviceResult]: ...

    def cancel(self) -> None: ...


class CollectorBackupService:
    """Orchestrate collection, durable storage, and run reporting."""

    def __init__(self, collector: CollectorProtocol) -> None:
        self.collector = collector

    def cancel(self) -> None:
        self.collector.cancel()

    def list_trusted_host_keys(self) -> tuple[object, ...]:
        store = getattr(self.collector, "host_key_store", None)
        list_method = getattr(store, "list_approved", None)
        if not callable(list_method):
            return ()
        return tuple(cast(Iterable[object], list_method()))

    def remove_trusted_host_keys(self, entries: Sequence[object]) -> None:
        store = getattr(self.collector, "host_key_store", None)
        remove_method = getattr(store, "remove", None)
        if not callable(remove_method):
            return
        for entry in entries:
            endpoint = str(_first_value(entry, "endpoint", "target.endpoint", default=""))
            if endpoint:
                remove_method(endpoint)

    @staticmethod
    def _failed_host_key_results(
        checks: Sequence[HostKeyCheck],
        *,
        code: ErrorCode | None = None,
        message: str | None = None,
        status: DeviceStatus = DeviceStatus.FAILED,
    ) -> list[DeviceResult]:
        now = datetime.now().astimezone()
        return [
            DeviceResult(
                target=check.target,
                status=status,
                attempts=check.attempts,
                started_at=now,
                finished_at=now,
                duration_seconds=0.0,
                error_code=code or check.error_code or ErrorCode.HOST_KEY_REJECTED,
                error_message=message or check.message,
            )
            for check in checks
        ]

    @staticmethod
    def _cancelled_results(targets: Sequence[DeviceTarget]) -> list[DeviceResult]:
        now = datetime.now().astimezone()
        return [
            DeviceResult(
                target=target,
                status=DeviceStatus.CANCELLED,
                attempts=0,
                started_at=now,
                finished_at=now,
                duration_seconds=0.0,
                error_code=ErrorCode.CANCELLED,
                error_message="호스트 키 확인 중 사용자가 실행을 취소했습니다.",
            )
            for target in targets
        ]

    @staticmethod
    def _report_record(result: DeviceResult, config_path: Path | None = None) -> dict[str, Any]:
        record = asdict(result)
        record["config_path"] = str(config_path) if config_path else ""
        record.pop("config_text", None)
        return record

    def _save_results(
        self,
        run_directory: Path,
        results: Sequence[DeviceResult],
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for result in results:
            config_path: Path | None = None
            if result.succeeded and result.config_text is not None:
                try:
                    config_path = device_config_path(
                        run_directory,
                        hostname=result.hostname,
                        ip_address=result.target.ip,
                    )
                    stored = write_config_atomic(config_path, result.config_text)
                    result.config_sha256 = stored.sha256
                    result.config_path = stored.path
                except OSError:
                    result.status = DeviceStatus.FAILED
                    result.error_code = ErrorCode.REPORT_WRITE_FAILED
                    result.error_message = (
                        "설정 파일을 저장하지 못했습니다. 출력 경로를 확인하세요."
                    )
                    result.config_path = None
                    config_path = None
                finally:
                    result.config_text = None
            records.append(self._report_record(result, config_path))
        return records

    def run(self, request: BackupRequest, callbacks: BackupCallbacks) -> BackupOutcome:
        self.collector.begin_run()
        started_at = datetime.now().astimezone()
        run_directory = create_run_directory(request.output_directory, now=started_at)
        logger = SanitizedJsonlLogger(
            run_directory / "operation.jsonl",
            sensitive_values=(
                *request.targets,
                request.username,
                request.password,
                request.enable_password or "",
            ),
        )
        targets = [DeviceTarget(ip, request.port) for ip in request.targets]
        credentials = Credentials(
            username=request.username,
            password=request.password,
            enable_secret=request.enable_password,
        )
        options = CollectionOptions(concurrency=request.concurrency, max_attempts=4)

        try:
            checks = self.collector.probe_host_keys(
                targets,
                options=options,
                cancel_event=callbacks.cancel_event,
            )
        except CollectionFailure as exc:
            if exc.code is not ErrorCode.CANCELLED:
                raise
            checks = []
            results = self._cancelled_results(targets)
        else:
            rejected = [check for check in checks if check.state is HostKeyTrustState.REJECTED]
            changed = [check for check in checks if check.state is HostKeyTrustState.CHANGED]
            unknown = [check for check in checks if check.state is HostKeyTrustState.UNKNOWN]
            trusted = [check for check in checks if check.state is HostKeyTrustState.TRUSTED]
            blocked_results = self._failed_host_key_results(rejected)

            if changed:
                callbacks.request_host_key_approval(changed)
                blocked_results.extend(
                    self._failed_host_key_results(
                        changed,
                        code=ErrorCode.HOST_KEY_CHANGED,
                        message="저장된 SSH 호스트 키와 현재 지문이 달라 해당 장비를 차단했습니다.",
                    )
                )
                logger.log(
                    stage="host_key_verification",
                    status="failed",
                    error_code=ErrorCode.HOST_KEY_CHANGED,
                    count=len(changed),
                    message="Changed SSH host keys were blocked.",
                )

            approved_unknown: list[HostKeyCheck] = []
            if unknown and callbacks.request_host_key_approval(unknown):
                self.collector.approve_host_keys(unknown)
                approved_unknown = unknown
            elif unknown:
                cancelled = callbacks.cancel_event.is_set()
                blocked_results.extend(
                    self._failed_host_key_results(
                        unknown,
                        code=ErrorCode.CANCELLED if cancelled else ErrorCode.HOST_KEY_REJECTED,
                        message=(
                            "호스트 키 확인 중 사용자가 실행을 취소했습니다."
                            if cancelled
                            else "사용자가 SSH 호스트 키 승인을 취소했습니다."
                        ),
                        status=DeviceStatus.CANCELLED if cancelled else DeviceStatus.FAILED,
                    )
                )
                logger.log(
                    stage="host_key_verification",
                    status="cancelled" if cancelled else "failed",
                    error_code=(ErrorCode.CANCELLED if cancelled else ErrorCode.HOST_KEY_REJECTED),
                    count=len(unknown),
                    message="SSH host key approval did not complete.",
                )

            eligible_targets = [check.target for check in (*trusted, *approved_unknown)]

            def forward_event(event: object) -> None:
                callbacks.on_event(event)
                logger.log(
                    stage=_first_value(event, "stage", default="collection"),
                    attempt=_first_value(event, "attempt", default=0),
                    error_code=_first_value(event, "error_code", default=None),
                    message=_first_value(event, "message", default=""),
                )

            collected_results = (
                self.collector.collect_many(
                    eligible_targets,
                    credentials,
                    options,
                    cancel_event=callbacks.cancel_event,
                    on_event=forward_event,
                )
                if eligible_targets
                else []
            )
            by_endpoint = {
                result.target.endpoint: result for result in (*blocked_results, *collected_results)
            }
            results = [by_endpoint[target.endpoint] for target in targets]

        records = self._save_results(run_directory, results)
        finished_at = datetime.now().astimezone()
        cancelled = callbacks.cancel_event.is_set() or any(
            result.status is DeviceStatus.CANCELLED for result in results
        )
        report_path = write_result_workbook(
            run_directory,
            records,
            summary=ReportSummary(
                started_at=started_at,
                finished_at=finished_at,
                cancelled=cancelled,
            ),
        )
        return BackupOutcome(
            run_directory=run_directory,
            results=tuple(records),
            report_path=report_path,
            cancelled=cancelled,
        )


@dataclass(slots=True)
class _ApprovalLatch:
    completed: threading.Event
    accepted: bool = False


class _ServiceWorker(QObject):
    event_received = Signal(object)
    host_keys_requested = Signal(object, object)
    succeeded = Signal(object)
    failed = Signal(str)
    done = Signal()

    def __init__(
        self,
        service: BackupServiceProtocol,
        request: BackupRequest,
        cancel_event: threading.Event,
    ) -> None:
        super().__init__()
        self._service = service
        self._request = request
        self._cancel_event = cancel_event

    def _request_approval(self, checks: Sequence[object]) -> bool:
        latch = _ApprovalLatch(threading.Event())
        self.host_keys_requested.emit(tuple(checks), latch)
        while not latch.completed.wait(timeout=0.1):
            if self._cancel_event.is_set():
                return False
        return latch.accepted

    @Slot()
    def run(self) -> None:
        callbacks = BackupCallbacks(
            on_event=self.event_received.emit,
            request_host_key_approval=self._request_approval,
            cancel_event=self._cancel_event,
        )
        try:
            outcome = self._service.run(self._request, callbacks)
        except Exception as exc:  # GUI boundary: show only the exception category.
            self.failed.emit(type(exc).__name__)
        else:
            self.succeeded.emit(outcome)
        finally:
            self.done.emit()


class HostKeyApprovalDialog(QDialog):
    """Review unknown SHA-256 fingerprints; changed keys are never approvable."""

    def __init__(self, checks: Sequence[object], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("SSH 호스트 키 확인")
        self.resize(820, 400)
        self._checks = tuple(checks)
        self.approval_allowed = not any(self._is_changed(check) for check in checks)

        layout = QVBoxLayout(self)
        explanation = QLabel(
            "인증 전에 장비가 제시한 SSH 호스트 키를 확인하세요. "
            "현장에 등록된 지문과 일치할 때만 모두 승인하십시오."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        table = QTableWidget(len(checks), 4, self)
        table.setObjectName("hostKeyTable")
        table.setHorizontalHeaderLabels(("장비", "키 유형", "SHA-256 지문", "상태"))
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for row, check in enumerate(checks):
            values = (
                _first_value(check, "target.endpoint", "target.ip", "ip"),
                _first_value(check, "observation.key_type", "key_type", "algorithm"),
                _first_value(check, "observation.fingerprint", "fingerprint", "sha256"),
                _first_value(check, "state", "status"),
            )
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(str(value)))
        layout.addWidget(table)

        if not self.approval_allowed:
            warning = QLabel(
                "저장된 키와 다른 지문이 감지되었습니다. 보안을 위해 이번 실행은 차단됩니다."
            )
            warning.setObjectName("hostKeyChangedWarning")
            warning.setStyleSheet("color: #b42318; font-weight: 600;")
            warning.setWordWrap(True)
            layout.addWidget(warning)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.approve_button = buttons.button(QDialogButtonBox.StandardButton.Yes)
        self.approve_button.setText("표시된 키 모두 승인")
        self.approve_button.setEnabled(self.approval_allowed)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _is_changed(check: object) -> bool:
        state = str(_first_value(check, "state", "status", default="")).lower()
        return "changed" in state or "mismatch" in state


class TrustedKeysDialog(QDialog):
    """Display and explicitly remove persisted known-host entries."""

    def __init__(
        self,
        entries: Sequence[object],
        remove_callback: Callable[[Sequence[object]], None] | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("신뢰 SSH 키 관리")
        self.resize(760, 380)
        self._entries = tuple(entries)
        self._remove_callback = remove_callback

        layout = QVBoxLayout(self)
        note = QLabel(
            "키를 제거하면 다음 접속 때 새 키로 다시 표시됩니다. "
            "키 변경 경고를 우회하기 위한 용도로 사용하지 마십시오."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = QTableWidget(len(entries), 4, self)
        self.table.setObjectName("trustedKeysTable")
        self.table.setHorizontalHeaderLabels(("장비", "키 유형", "SHA-256 지문", "저장 시각"))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for row, entry in enumerate(entries):
            values = (
                _first_value(entry, "target.endpoint", "endpoint", "host"),
                _first_value(entry, "key_type", "algorithm"),
                _first_value(entry, "fingerprint", "sha256"),
                _first_value(entry, "approved_at", "saved_at", "created_at"),
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        layout.addWidget(self.table)

        actions = QHBoxLayout()
        remove_button = QPushButton("선택 키 제거", self)
        remove_button.setObjectName("removeTrustedKeyButton")
        remove_button.setEnabled(remove_callback is not None)
        remove_button.clicked.connect(self._remove_selected)
        close_button = QPushButton("닫기", self)
        close_button.clicked.connect(self.accept)
        actions.addWidget(remove_button)
        actions.addStretch(1)
        actions.addWidget(close_button)
        layout.addLayout(actions)

    @Slot()
    def _remove_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        if not rows or self._remove_callback is None:
            return
        answer = QMessageBox.question(
            self,
            "신뢰 키 제거",
            f"선택한 {len(rows)}개 키를 제거하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        self._remove_callback(tuple(self._entries[row] for row in rows))
        self.accept()


class MainWindow(QMainWindow):
    """Single-window, repeatable operations UI."""

    RESULT_COLUMNS = ("IP", "호스트명", "모델/SKU", "상태", "시도", "오류")

    def __init__(
        self,
        service: BackupServiceProtocol | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._thread: QThread | None = None
        self._worker: _ServiceWorker | None = None
        self._cancel_thread: threading.Thread | None = None
        self._cancel_event: threading.Event | None = None
        self._result_directory: Path | None = None
        self._row_by_target: dict[str, int] = {}
        self._completed_targets: set[str] = set()
        self._target_count = 0
        self._pending_error: str | None = None
        self._closing_after_cancel = False
        self._close_retry_scheduled = False
        self._run_finalize_pending = False
        self._run_finalize_retry_scheduled = False

        self.setWindowTitle("Aruba 2930F 설정 백업")
        self.resize(1040, 760)
        self.setMinimumSize(840, 640)
        self._build_ui()
        self._set_running(False)

    def set_service(self, service: BackupServiceProtocol) -> None:
        if self._thread is not None:
            raise RuntimeError("실행 중에는 백업 서비스를 변경할 수 없습니다.")
        self._service = service

    def _build_ui(self) -> None:
        central = QWidget(self)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        target_group = QGroupBox("1. 대상 장비", central)
        target_layout = QGridLayout(target_group)
        target_layout.addWidget(QLabel("IPv4 주소 (한 줄에 하나)"), 0, 0, 1, 2)
        self.ip_input = QPlainTextEdit(target_group)
        self.ip_input.setObjectName("ipInput")
        self.ip_input.setPlaceholderText("192.0.2.10\n192.0.2.11")
        self.ip_input.setMinimumHeight(110)
        target_layout.addWidget(self.ip_input, 1, 0, 1, 2)
        target_layout.addWidget(QLabel("SSH 포트"), 0, 2)
        self.port_input = QSpinBox(target_group)
        self.port_input.setObjectName("portInput")
        self.port_input.setRange(1, 65_535)
        self.port_input.setValue(22)
        target_layout.addWidget(
            self.port_input,
            1,
            2,
            alignment=Qt.AlignmentFlag.AlignTop,
        )
        target_layout.setColumnStretch(1, 1)
        outer.addWidget(target_group)

        access_group = QGroupBox("2. 공통 접속 정보", central)
        access_layout = QFormLayout(access_group)
        self.username_input = QLineEdit(access_group)
        self.username_input.setObjectName("usernameInput")
        self.password_input = QLineEdit(access_group)
        self.password_input.setObjectName("passwordInput")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.enable_password_input = QLineEdit(access_group)
        self.enable_password_input.setObjectName("enablePasswordInput")
        self.enable_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.enable_password_input.setPlaceholderText("필요한 경우에만 입력")
        access_layout.addRow("사용자 이름", self.username_input)
        access_layout.addRow("비밀번호", self.password_input)
        access_layout.addRow("Enable 암호 (선택)", self.enable_password_input)
        outer.addWidget(access_group)

        options_group = QGroupBox("3. 실행 옵션", central)
        options_layout = QGridLayout(options_group)
        options_layout.addWidget(QLabel("동시 접속 수"), 0, 0)
        self.concurrency_input = QSpinBox(options_group)
        self.concurrency_input.setObjectName("concurrencyInput")
        self.concurrency_input.setRange(1, 20)
        self.concurrency_input.setValue(10)
        options_layout.addWidget(self.concurrency_input, 0, 1)
        options_layout.addWidget(QLabel("결과 저장 위치"), 1, 0)
        self.output_input = QLineEdit(str(default_output_directory()), options_group)
        self.output_input.setObjectName("outputInput")
        options_layout.addWidget(self.output_input, 1, 1)
        self.browse_button = QPushButton("찾아보기…", options_group)
        self.browse_button.setObjectName("browseOutputButton")
        self.browse_button.clicked.connect(self._browse_output)
        options_layout.addWidget(self.browse_button, 1, 2)
        self.trust_keys_button = QPushButton("신뢰 키 관리…", options_group)
        self.trust_keys_button.setObjectName("trustKeysButton")
        self.trust_keys_button.clicked.connect(self._manage_trusted_keys)
        options_layout.addWidget(self.trust_keys_button, 0, 2)
        options_layout.setColumnStretch(1, 1)
        outer.addWidget(options_group)

        action_layout = QHBoxLayout()
        self.start_button = QPushButton("백업 시작", central)
        self.start_button.setObjectName("startButton")
        self.start_button.setDefault(True)
        self.start_button.clicked.connect(self._start_backup)
        self.cancel_button = QPushButton("취소", central)
        self.cancel_button.setObjectName("cancelButton")
        self.cancel_button.clicked.connect(self._cancel_backup)
        self.open_result_button = QPushButton("결과 폴더 열기", central)
        self.open_result_button.setObjectName("openResultButton")
        self.open_result_button.clicked.connect(self._open_result_directory)
        action_layout.addWidget(self.start_button)
        action_layout.addWidget(self.cancel_button)
        action_layout.addStretch(1)
        action_layout.addWidget(self.open_result_button)
        outer.addLayout(action_layout)

        self.status_label = QLabel("대기 중", central)
        self.status_label.setObjectName("statusLabel")
        self.progress_bar = QProgressBar(central)
        self.progress_bar.setObjectName("progressBar")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        outer.addWidget(self.status_label)
        outer.addWidget(self.progress_bar)

        self.result_table = QTableWidget(0, len(self.RESULT_COLUMNS), central)
        self.result_table.setObjectName("resultTable")
        self.result_table.setHorizontalHeaderLabels(self.RESULT_COLUMNS)
        self.result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.result_table.setAlternatingRowColors(True)
        self.result_table.verticalHeader().setVisible(False)
        header = self.result_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        outer.addWidget(self.result_table, stretch=1)
        self.setCentralWidget(central)

    @staticmethod
    def parse_targets(text: str) -> tuple[str, ...]:
        targets: list[str] = []
        seen: set[str] = set()
        errors: list[str] = []
        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            candidate = raw_line.strip()
            if not candidate:
                continue
            try:
                normalized = str(ipaddress.IPv4Address(candidate))
            except ipaddress.AddressValueError:
                errors.append(f"{line_number}행: 올바른 IPv4 주소가 아닙니다.")
                continue
            if normalized in seen:
                errors.append(f"{line_number}행: 중복 IPv4 주소입니다.")
                continue
            seen.add(normalized)
            targets.append(normalized)
        if not targets and not errors:
            errors.append("대상 IPv4 주소를 한 개 이상 입력하세요.")
        if errors:
            raise ValueError("\n".join(errors))
        return tuple(targets)

    def build_request(self) -> BackupRequest:
        targets = self.parse_targets(self.ip_input.toPlainText())
        username = self.username_input.text().strip()
        password = self.password_input.text()
        output_text = self.output_input.text().strip()
        if not username:
            raise ValueError("사용자 이름을 입력하세요.")
        if not password:
            raise ValueError("비밀번호를 입력하세요.")
        if not output_text:
            raise ValueError("결과 저장 위치를 선택하세요.")
        return BackupRequest(
            targets=targets,
            port=self.port_input.value(),
            username=username,
            password=password,
            enable_password=self.enable_password_input.text() or None,
            concurrency=self.concurrency_input.value(),
            output_directory=Path(output_text).expanduser(),
        )

    @Slot()
    def _browse_output(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "결과 저장 위치 선택",
            self.output_input.text(),
        )
        if selected:
            self.output_input.setText(selected)

    @Slot()
    def _start_backup(self) -> None:
        if self._thread is not None:
            return
        if self._service is None:
            try:
                self._service = build_default_service()
            except Exception:
                QMessageBox.critical(self, "시작할 수 없음", "백업 서비스를 초기화하지 못했습니다.")
                return
        try:
            request = self.build_request()
        except ValueError as exc:
            QMessageBox.warning(self, "입력 확인", str(exc))
            return

        self._target_count = len(request.targets)
        self._row_by_target.clear()
        self._completed_targets.clear()
        self.result_table.setRowCount(0)
        self.progress_bar.setValue(0)
        self._result_directory = None
        self._pending_error = None
        self._cancel_event = threading.Event()
        self._set_running(True)
        self.status_label.setText(f"{self._target_count}대 백업을 준비하는 중…")

        thread = QThread(self)
        worker = _ServiceWorker(self._service, request, self._cancel_event)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.event_received.connect(self._on_collection_event)
        worker.host_keys_requested.connect(self._on_host_keys_requested)
        worker.succeeded.connect(self._on_worker_success)
        worker.failed.connect(self._on_worker_failure)
        worker.done.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_thread_finished)
        self._thread = thread
        self._worker = worker
        thread.start()

    @Slot()
    def _cancel_backup(self) -> None:
        if self._thread is None or self._cancel_event is None:
            return
        self._cancel_event.set()
        self.cancel_button.setEnabled(False)
        self.status_label.setText("취소 요청을 처리하는 중…")
        if self._service is not None and (
            self._cancel_thread is None or not self._cancel_thread.is_alive()
        ):
            self._cancel_thread = threading.Thread(
                target=self._cancel_service_off_thread,
                name="aruba2930f-cancel",
                daemon=True,
            )
            self._cancel_thread.start()

    def _cancel_service_off_thread(self) -> None:
        service = self._service
        if service is None:
            return
        with suppress(Exception):
            service.cancel()

    @Slot(object, object)
    def _on_host_keys_requested(self, checks: object, latch: object) -> None:
        approval_latch = cast(_ApprovalLatch, latch)
        if self._cancel_event is not None and self._cancel_event.is_set():
            approval_latch.accepted = False
            approval_latch.completed.set()
            return
        dialog = HostKeyApprovalDialog(cast(Sequence[object], checks), self)
        approval_latch.accepted = (
            dialog.exec() == QDialog.DialogCode.Accepted and dialog.approval_allowed
        )
        approval_latch.completed.set()

    def _row_for_target(self, target: str) -> int:
        if target in self._row_by_target:
            return self._row_by_target[target]
        row = self.result_table.rowCount()
        self.result_table.insertRow(row)
        self._row_by_target[target] = row
        for column in range(self.result_table.columnCount()):
            self.result_table.setItem(row, column, QTableWidgetItem(""))
        self._set_cell(row, 0, target)
        return row

    def _set_cell(self, row: int, column: int, value: Any) -> None:
        item = self.result_table.item(row, column)
        if item is None:
            item = QTableWidgetItem("")
            self.result_table.setItem(row, column, item)
        item.setText("" if value is None else str(value))

    @Slot(object)
    def _on_collection_event(self, event: object) -> None:
        target = str(_first_value(event, "target.ip", "ip", default="알 수 없음"))
        stage = str(_first_value(event, "stage", default="running"))
        attempt = _first_value(event, "attempt", default="")
        message = _first_value(event, "message", default="")
        error_code = _first_value(event, "error_code", default="")
        row = self._row_for_target(target)
        self._set_cell(row, 3, stage)
        self._set_cell(row, 4, attempt)
        self._set_cell(row, 5, error_code or message)
        if stage.lower() in {"completed", "success", "failed", "cancelled"}:
            self._completed_targets.add(target)
        completed = len(self._completed_targets)
        if self._target_count:
            self.progress_bar.setValue(round(completed * 100 / self._target_count))
        self.status_label.setText(f"진행 중: {completed}/{self._target_count}대 완료")

    @Slot(object)
    def _on_worker_success(self, outcome: object) -> None:
        self._result_directory = Path(str(_first_value(outcome, "run_directory")))
        results = cast(Iterable[object], _first_value(outcome, "results", default=()))
        for result in results:
            target = str(_first_value(result, "target.ip", "ip_address", "ip"))
            row = self._row_for_target(target)
            model = _first_value(result, "model", default="")
            sku = _first_value(result, "sku", default="")
            self._set_cell(row, 1, _first_value(result, "hostname"))
            self._set_cell(row, 2, " / ".join(part for part in (str(model), str(sku)) if part))
            self._set_cell(row, 3, _first_value(result, "status"))
            self._set_cell(row, 4, _first_value(result, "attempts", default=0))
            error = _first_value(result, "error_code", default="")
            message = _first_value(result, "error_message", default="")
            self._set_cell(row, 5, " - ".join(part for part in (str(error), str(message)) if part))
        self.progress_bar.setValue(100)
        cancelled = bool(_first_value(outcome, "cancelled", default=False))
        self.status_label.setText(
            "취소된 실행의 결과를 저장했습니다." if cancelled else "백업이 완료되었습니다."
        )

    @Slot(str)
    def _on_worker_failure(self, exception_name: str) -> None:
        self._pending_error = exception_name
        self.status_label.setText("백업 실행을 완료하지 못했습니다.")

    @Slot()
    def _on_thread_finished(self) -> None:
        thread = self._thread
        self._thread = None
        self._worker = None
        self._cancel_event = None
        if thread is not None:
            thread.deleteLater()
        if self._cancel_thread_is_alive():
            self._run_finalize_pending = True
            self._schedule_run_finalize()
            return
        self._finalize_completed_run()

    def _schedule_run_finalize(self) -> None:
        if self._run_finalize_retry_scheduled:
            return
        self._run_finalize_retry_scheduled = True
        QTimer.singleShot(50, self._finish_run_after_cancel)

    @Slot()
    def _finish_run_after_cancel(self) -> None:
        self._run_finalize_retry_scheduled = False
        if self._cancel_thread_is_alive():
            self._schedule_run_finalize()
            return
        self._cancel_thread = None
        self._finalize_completed_run()

    def _finalize_completed_run(self) -> None:
        self._cancel_thread = None
        self._run_finalize_pending = False
        self._set_running(False)
        self.password_input.clear()
        self.enable_password_input.clear()
        if self._pending_error:
            QMessageBox.critical(
                self,
                "백업 실패",
                f"백업 처리 중 오류가 발생했습니다. 오류 유형: {self._pending_error}",
            )
            self._pending_error = None
        if self._closing_after_cancel:
            self._schedule_close_retry()

    def _cancel_thread_is_alive(self) -> bool:
        return self._cancel_thread is not None and self._cancel_thread.is_alive()

    def _schedule_close_retry(self) -> None:
        if self._close_retry_scheduled:
            return
        self._close_retry_scheduled = True
        QTimer.singleShot(50, self._retry_close_after_cancel)

    @Slot()
    def _retry_close_after_cancel(self) -> None:
        self._close_retry_scheduled = False
        if self._thread is not None or self._cancel_thread_is_alive() or self._run_finalize_pending:
            self._schedule_close_retry()
            return
        self._cancel_thread = None
        self._closing_after_cancel = False
        self.close()

    def _set_running(self, running: bool) -> None:
        for widget in (
            self.ip_input,
            self.port_input,
            self.username_input,
            self.password_input,
            self.enable_password_input,
            self.concurrency_input,
            self.output_input,
            self.browse_button,
            self.trust_keys_button,
        ):
            widget.setEnabled(not running)
        self.start_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.open_result_button.setEnabled(not running and self._result_directory is not None)

    @Slot()
    def _open_result_directory(self) -> None:
        if self._result_directory is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._result_directory)))

    @Slot()
    def _manage_trusted_keys(self) -> None:
        if self._service is None:
            try:
                self._service = build_default_service()
            except Exception:
                QMessageBox.critical(self, "키 관리", "신뢰 키 저장소를 열지 못했습니다.")
                return
        list_method = getattr(self._service, "list_trusted_host_keys", None)
        remove_method = getattr(self._service, "remove_trusted_host_keys", None)
        if not callable(list_method):
            QMessageBox.information(self, "키 관리", "저장된 신뢰 키가 없습니다.")
            return
        entries = tuple(list_method())
        dialog = TrustedKeysDialog(
            entries,
            cast(
                Callable[[Sequence[object]], None] | None,
                remove_method if callable(remove_method) else None,
            ),
            self,
        )
        dialog.exec()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._thread is not None or self._cancel_thread_is_alive() or self._run_finalize_pending:
            self._closing_after_cancel = True
            if self._thread is not None:
                self._cancel_backup()
            self._schedule_close_retry()
            event.ignore()
            return
        self._cancel_thread = None
        self.ip_input.clear()
        self.username_input.clear()
        self.password_input.clear()
        self.enable_password_input.clear()
        super().closeEvent(event)


def build_default_service() -> BackupServiceProtocol:
    """Create the production collector lazily so GUI smoke tests stay offline."""

    from .collector import ArubaCollector

    return CollectorBackupService(cast(CollectorProtocol, ArubaCollector()))


def run_gui(service: BackupServiceProtocol | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Aruba2930FConfigBackup")
    app.setOrganizationName("sebia1993")
    window = MainWindow(service=service)
    window.show()
    return app.exec()
