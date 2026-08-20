from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QTableWidgetItem

from aruba2930f_backup import __main__ as entrypoint
from aruba2930f_backup.developer_inspector import (
    DeveloperInspectorBar,
    DeveloperInspectorController,
)
from aruba2930f_backup.gui import (
    DiagnosticCodesDialog,
    HostKeyApprovalDialog,
    MainWindow,
    TrustedKeysDialog,
)


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _ids(controller: DeveloperInspectorController) -> set[str]:
    return {metadata.stable_id for metadata in controller.catalog}


def test_no_cli_or_environment_activation_path_exists(
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARUBA2930F_UI_INSPECTOR", "1")
    with pytest.raises(SystemExit) as exc_info:
        entrypoint._parser().parse_args(["--ui-inspector"])
    assert exc_info.value.code == 2

    controller = DeveloperInspectorController(app, "v0.1.5")
    try:
        assert controller.enabled is False
        assert not hasattr(controller, "enable")
        assert not hasattr(controller, "toggle")
    finally:
        controller.close()


@pytest.mark.gui
def test_main_window_registers_fixed_surfaces_and_f12_controls_bar(
    app: QApplication,
) -> None:
    controller = DeveloperInspectorController(app, "v0.1.5")
    window = MainWindow(developer_inspector=controller)
    window.show()
    app.processEvents()

    try:
        bar = window.findChild(DeveloperInspectorBar)
        assert bar is not None
        assert bar.isVisible() is False
        assert window.property("uiInspectorId") == "MAIN-WINDOW"
        assert window.result_table.viewport().property("uiInspectorId") == "RESULT-TABLE-BODY"
        assert window.result_table.horizontalHeader().property("uiInspectorId") == (
            "RESULT-TABLE-HEADER"
        )
        assert {
            "MAIN-WINDOW",
            "BACKUP-TARGET-SECTION",
            "BACKUP-TARGETS",
            "BACKUP-SSH-PORT",
            "BACKUP-ACCESS-SECTION",
            "BACKUP-USERNAME",
            "BACKUP-PASSWORD",
            "BACKUP-ENABLE-PASSWORD",
            "BACKUP-OPTIONS-SECTION",
            "BACKUP-CONCURRENCY",
            "BACKUP-OUTPUT-DIRECTORY",
            "BACKUP-OUTPUT-BROWSE",
            "HOSTKEY-MANAGEMENT",
            "BACKUP-START",
            "BACKUP-CANCEL",
            "BACKUP-RETRY-EXHAUSTED",
            "BACKUP-OPEN-RESULT",
            "BACKUP-STATUS",
            "BACKUP-PROGRESS",
            "RESULT-TABLE",
            "RESULT-TABLE-BODY",
            "RESULT-TABLE-HEADER",
        } <= _ids(controller)

        QTest.keyClick(window, Qt.Key_F12)
        app.processEvents()
        assert controller.enabled is True
        assert bar.isVisible() is True

        QTest.keyClick(window, Qt.Key_F12)
        app.processEvents()
        assert controller.enabled is False
        assert bar.isVisible() is False
    finally:
        window.close()
        controller.close()
        app.processEvents()


@pytest.mark.gui
def test_selection_of_start_button_never_runs_its_action(app: QApplication) -> None:
    controller = DeveloperInspectorController(app, "v0.1.5")
    window = MainWindow(developer_inspector=controller)
    clicked = QSignalSpy(window.start_button.clicked)
    window.show()
    app.processEvents()

    try:
        QTest.keyClick(window.start_button, Qt.Key_F12)
        assert controller.begin_selection()
        QTest.mouseClick(window.start_button, Qt.LeftButton)
        app.processEvents()

        assert clicked.count() == 0
        assert controller.detail_dialog is not None
        assert controller.detail_dialog.metadata is not None
        assert controller.detail_dialog.metadata.stable_id == "BACKUP-START"
    finally:
        window.close()
        controller.close()
        app.processEvents()


@pytest.mark.gui
def test_one_controller_drives_all_custom_dialog_bars_and_static_catalog(
    app: QApplication,
) -> None:
    controller = DeveloperInspectorController(app, "v0.1.5")
    window = MainWindow(developer_inspector=controller)
    window.ip_input.setPlainText("198.51.100.81")
    window.username_input.setText("runtime-operator")
    window.password_input.setText("runtime-password-value")
    window.enable_password_input.setText("runtime-enable-value")
    window.result_table.insertRow(0)
    window.result_table.setItem(0, 0, QTableWidgetItem("198.51.100.82"))
    window.result_table.setItem(0, 5, QTableWidgetItem("runtime-error-message"))

    diagnostic = DiagnosticCodesDialog(
        {"A3F1-010EPMRC-3": 2},
        parent=window,
        developer_inspector=controller,
    )
    approval = HostKeyApprovalDialog(
        [
            {
                "target": {"endpoint": "198.51.100.83:22"},
                "key_type": "ssh-ed25519-runtime",
                "fingerprint": "SHA256:runtime-fingerprint",
                "state": "changed",
            }
        ],
        window,
        developer_inspector=controller,
    )
    trusted = TrustedKeysDialog(
        [
            {
                "endpoint": "198.51.100.84:22",
                "key_type": "ssh-rsa-runtime",
                "fingerprint": "SHA256:trusted-runtime-fingerprint",
                "approved_at": "runtime-time",
            }
        ],
        None,
        window,
        developer_inspector=controller,
    )
    windows = (window, diagnostic, approval, trusted)
    for item in windows:
        item.show()
    app.processEvents()

    try:
        bars = [item.findChild(DeveloperInspectorBar) for item in windows]
        assert all(bar is not None for bar in bars)
        assert all(not bar.isVisible() for bar in bars if bar is not None)
        expected_dialog_ids = {
            "DIAGNOSTIC-CODES-DIALOG",
            "DIAGNOSTIC-CODES-TEXT",
            "DIAGNOSTIC-CODES-COPY",
            "DIAGNOSTIC-CODES-CLOSE",
            "HOSTKEY-APPROVAL-DIALOG",
            "HOSTKEY-APPROVAL-TABLE",
            "HOSTKEY-APPROVAL-TABLE-BODY",
            "HOSTKEY-APPROVAL-TABLE-HEADER",
            "HOSTKEY-APPROVAL-WARNING",
            "HOSTKEY-APPROVAL-ACCEPT",
            "HOSTKEY-APPROVAL-CANCEL",
            "HOSTKEY-TRUSTED-DIALOG",
            "HOSTKEY-TRUSTED-TABLE",
            "HOSTKEY-TRUSTED-TABLE-BODY",
            "HOSTKEY-TRUSTED-TABLE-HEADER",
            "HOSTKEY-TRUSTED-REMOVE",
            "HOSTKEY-TRUSTED-CLOSE",
        }
        assert expected_dialog_ids <= _ids(controller)

        QTest.keyClick(approval, Qt.Key_F12)
        app.processEvents()
        assert controller.enabled is True
        assert all(bar.isVisible() for bar in bars if bar is not None)

        catalog_text = "\n".join(
            "|".join(
                (
                    metadata.name_ko,
                    metadata.stable_id,
                    metadata.screen_path,
                    metadata.source_path,
                    metadata.purpose,
                    controller.request_text(metadata),
                )
            )
            for metadata in controller.catalog
        )
        for runtime_value in (
            "198.51.100.81",
            "198.51.100.82",
            "198.51.100.83",
            "198.51.100.84",
            "runtime-operator",
            "runtime-password-value",
            "runtime-enable-value",
            "runtime-error-message",
            "runtime-fingerprint",
            "trusted-runtime-fingerprint",
            "A3F1-010EPMRC-3",
        ):
            assert runtime_value not in catalog_text

        password_metadata = next(
            metadata for metadata in controller.catalog if metadata.stable_id == "BACKUP-PASSWORD"
        )
        detail = controller.show_element_detail(password_metadata, window)
        assert detail is not None
        copied = detail.copy_request()
        assert copied == QApplication.clipboard().text()
        assert "runtime-password-value" not in copied
        assert "BACKUP-PASSWORD" in copied

        QTest.keyClick(trusted, Qt.Key_F12)
        app.processEvents()
        assert controller.enabled is False
        assert all(not bar.isVisible() for bar in bars if bar is not None)
    finally:
        trusted.close()
        approval.close()
        diagnostic.close()
        window.close()
        controller.close()
        app.processEvents()


@pytest.mark.gui
def test_existing_constructors_remain_inspector_free_and_compatible(
    app: QApplication,
) -> None:
    window = MainWindow()
    diagnostic = DiagnosticCodesDialog({"A3F1-010EPMRC-3": 1})
    approval = HostKeyApprovalDialog([])
    trusted = TrustedKeysDialog([], None)
    try:
        assert window.developer_inspector is None
        assert diagnostic.developer_inspector is None
        assert approval.developer_inspector is None
        assert trusted.developer_inspector is None
        assert window.findChild(DeveloperInspectorBar) is None
        assert diagnostic.findChild(DeveloperInspectorBar) is None
        assert approval.findChild(DeveloperInspectorBar) is None
        assert trusted.findChild(DeveloperInspectorBar) is None
    finally:
        trusted.close()
        approval.close()
        diagnostic.close()
        window.close()
        app.processEvents()
