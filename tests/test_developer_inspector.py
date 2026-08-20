from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer
from PySide6.QtGui import QAction, QContextMenuEvent, QKeyEvent, QKeySequence, QMouseEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from aruba2930f_backup.developer_inspector import (
    DeveloperInspectorController,
    UiElementMetadata,
    build_static_request_text,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _metadata(
    stable_id: str = "MAIN-FULL-DEVICE-TABLE-SELECTION",
    *,
    name: str = "전체 보기 장비표의 선택된 행",
    screen: str = "메인 화면 > 전체 보기 > 장비 상태 표",
    source: str = "src/aruba2930f_backup/gui.py",
    purpose: str = "현재 작업 대상으로 선택된 장비 행을 표시합니다.",
) -> UiElementMetadata:
    return UiElementMetadata(name, stable_id, screen, source, purpose)


@pytest.fixture
def inspector() -> DeveloperInspectorController:
    controller = DeveloperInspectorController(_app(), "v0.1.7")
    yield controller
    controller.close()
    _app().processEvents()


def test_catalog_metadata_requires_fixed_id_and_repository_relative_source() -> None:
    metadata = _metadata()
    assert metadata.stable_id == "MAIN-FULL-DEVICE-TABLE-SELECTION"
    assert metadata.source_path.startswith("src/")

    with pytest.raises(ValueError, match="uppercase ASCII"):
        _metadata("main-table")
    with pytest.raises(ValueError, match="repository-relative"):
        _metadata(source="D:/private/checkout/main_window.py")
    with pytest.raises(ValueError, match="repository-relative"):
        _metadata(source="file:///C:/private/checkout/main_window.py")
    with pytest.raises(ValueError, match="repository-relative"):
        _metadata(source="https://example.invalid/main_window.py")
    with pytest.raises(ValueError, match="repository-relative"):
        _metadata(source="src//aruba_mini_dashboard/ui/main_window.py")
    with pytest.raises(ValueError, match="repository-relative"):
        _metadata(source="src/./aruba_mini_dashboard/ui/main_window.py")
    with pytest.raises(ValueError, match="repository-relative"):
        _metadata(source="../main_window.py")
    with pytest.raises(ValueError, match="single printable line"):
        _metadata(name="장비표\n192.0.2.10")


def test_f12_is_the_only_enable_path_and_each_controller_starts_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app()
    monkeypatch.setenv("ARUBA2930F_UI_INSPECTOR", "1")
    host = QWidget()
    layout = QVBoxLayout(host)
    controller = DeveloperInspectorController(app, "v0.1.7")
    bar = controller.attach_host_layout(host, layout)
    f12_action = QAction(host)
    f12_action.setShortcut(QKeySequence("F12"))
    host.addAction(f12_action)
    action_spy = QSignalSpy(f12_action.triggered)
    host.show()
    app.processEvents()

    try:
        assert controller.enabled is False
        assert bar.isVisible() is False
        assert controller.begin_selection() is False
        assert controller.show_catalog() is None
        assert not hasattr(controller, "enable")
        assert not hasattr(controller, "toggle")

        QTest.keyClick(host, Qt.Key_F11)
        QTest.keyClick(host, Qt.Key_F12, Qt.ControlModifier)
        QTest.keyClick(host, Qt.Key_F12, Qt.ShiftModifier)
        QTest.keyClick(host, Qt.Key_F12, Qt.AltModifier)
        QApplication.sendEvent(
            host,
            QKeyEvent(
                QEvent.KeyPress,
                Qt.Key_F12,
                Qt.NoModifier,
                "",
                True,
                2,
            ),
        )
        assert controller.enabled is False

        QTest.keyClick(host, Qt.Key_F12)
        app.processEvents()
        assert controller.enabled is True
        assert bar.isVisible() is True
        assert action_spy.count() == 0

        QTest.keyClick(host, Qt.Key_F12)
        app.processEvents()
        assert controller.enabled is False
        assert bar.isVisible() is False
    finally:
        controller.close()
        host.close()

    replacement = DeveloperInspectorController(app, "v0.1.7")
    try:
        assert replacement.enabled is False
    finally:
        replacement.close()


def test_exit_bar_can_only_disable(inspector: DeveloperInspectorController) -> None:
    app = _app()
    host = QWidget()
    layout = QVBoxLayout(host)
    bar = inspector.attach_host_layout(host, layout)
    host.show()
    app.processEvents()

    QTest.keyClick(host, Qt.Key_F12)
    assert inspector.enabled
    assert inspector.begin_selection()
    QTest.mouseClick(bar.exit_button, Qt.LeftButton)
    assert inspector.enabled is False
    assert inspector.selection_mode is False
    assert inspector.begin_selection() is False
    QTest.mouseClick(bar.exit_button, Qt.LeftButton)
    assert inspector.enabled is False
    host.close()


def test_selection_climbs_to_registered_parent_and_consumes_entire_click(
    inspector: DeveloperInspectorController,
) -> None:
    app = _app()
    host = QWidget()
    root = QVBoxLayout(host)
    registered_parent = QFrame(host)
    child_layout = QVBoxLayout(registered_parent)
    runtime_button = QPushButton("192.0.2.10 비밀 별칭", registered_parent)
    child_layout.addWidget(runtime_button)
    root.addWidget(registered_parent)
    metadata = _metadata()
    inspector.register_widget(registered_parent, metadata)
    selected_spy = QSignalSpy(inspector.element_selected)
    clicked_spy = QSignalSpy(runtime_button.clicked)
    pressed_spy = QSignalSpy(runtime_button.pressed)
    host.show()
    app.processEvents()

    QTest.keyClick(runtime_button, Qt.Key_F12)
    assert inspector.begin_selection()
    QTest.mouseMove(runtime_button, runtime_button.rect().center())
    app.processEvents()
    assert inspector.hovered_metadata == metadata

    QTest.mouseClick(runtime_button, Qt.LeftButton)
    app.processEvents()
    assert selected_spy.count() == 1
    assert selected_spy.at(0)[0] == metadata
    assert pressed_spy.count() == 0
    assert clicked_spy.count() == 0
    assert inspector.selection_mode is False
    assert inspector.detail_dialog is not None
    assert inspector.detail_dialog.metadata == metadata

    QTest.mouseClick(runtime_button, Qt.LeftButton)
    assert clicked_spy.count() == 1
    host.close()


def test_missing_selected_release_never_swallows_the_next_normal_click(
    inspector: DeveloperInspectorController,
) -> None:
    app = _app()
    host = QWidget()
    layout = QVBoxLayout(host)
    selected = QPushButton("선택 대상", host)
    normal = QPushButton("다음 정상 동작", host)
    layout.addWidget(selected)
    layout.addWidget(normal)
    inspector.register_widget(selected, _metadata("MAIN-ORPHAN-PRESS-TARGET"))
    normal_clicked = QSignalSpy(normal.clicked)
    host.show()
    app.processEvents()

    QTest.keyClick(selected, Qt.Key_F12)
    assert inspector.begin_selection()
    QApplication.sendEvent(
        selected,
        QMouseEvent(
            QEvent.MouseButtonPress,
            selected.rect().center(),
            selected.mapToGlobal(selected.rect().center()),
            Qt.LeftButton,
            Qt.LeftButton,
            Qt.NoModifier,
        ),
    )
    assert inspector.selection_mode is False

    QTest.mouseClick(normal, Qt.LeftButton)
    assert normal_clicked.count() == 1
    assert normal.isDown() is False
    host.close()


def test_double_click_tail_never_executes_the_selected_button(
    inspector: DeveloperInspectorController,
) -> None:
    app = _app()
    target = QPushButton("더블클릭 선택 대상")
    inspector.register_widget(target, _metadata("MAIN-DOUBLE-CLICK-TARGET"))
    pressed = QSignalSpy(target.pressed)
    released = QSignalSpy(target.released)
    clicked = QSignalSpy(target.clicked)
    selected = QSignalSpy(inspector.element_selected)
    target.show()
    app.processEvents()
    point = target.rect().center()
    global_point = target.mapToGlobal(point)

    QTest.keyClick(target, Qt.Key_F12)
    assert inspector.begin_selection()
    for event_type, buttons in (
        (QEvent.MouseButtonPress, Qt.LeftButton),
        (QEvent.MouseButtonRelease, Qt.NoButton),
        (QEvent.MouseButtonDblClick, Qt.LeftButton),
        (QEvent.MouseButtonRelease, Qt.NoButton),
    ):
        QApplication.sendEvent(
            target,
            QMouseEvent(
                event_type,
                point,
                global_point,
                Qt.LeftButton,
                buttons,
                Qt.NoModifier,
            ),
        )

    assert selected.count() == 1
    assert pressed.count() == 0
    assert released.count() == 0
    assert clicked.count() == 0
    assert target.isDown() is False
    assert not inspector._suppressed_mouse_buttons
    target.close()


def test_direct_double_click_can_select_without_running_original_action(
    inspector: DeveloperInspectorController,
) -> None:
    app = _app()
    target = QPushButton("직접 더블클릭 대상")
    metadata = _metadata("MAIN-DIRECT-DOUBLE-CLICK-TARGET")
    inspector.register_widget(target, metadata)
    clicked = QSignalSpy(target.clicked)
    selected = QSignalSpy(inspector.element_selected)
    target.show()
    app.processEvents()

    QTest.keyClick(target, Qt.Key_F12)
    assert inspector.begin_selection()
    point = target.rect().center()
    global_point = target.mapToGlobal(point)
    for event_type, buttons in (
        (QEvent.MouseButtonDblClick, Qt.LeftButton),
        (QEvent.MouseButtonRelease, Qt.NoButton),
    ):
        QApplication.sendEvent(
            target,
            QMouseEvent(
                event_type,
                point,
                global_point,
                Qt.LeftButton,
                buttons,
                Qt.NoModifier,
            ),
        )

    assert selected.count() == 1
    assert selected.at(0)[0] == metadata
    assert clicked.count() == 0
    assert inspector.selection_mode is False
    target.close()


def test_host_bar_weak_references_are_pruned_on_later_attachment(
    inspector: DeveloperInspectorController,
) -> None:
    app = _app()
    for _ in range(20):
        host = QWidget()
        inspector.attach_host_layout(host, QVBoxLayout(host))
        host.deleteLater()
        del host
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()

    live_host = QWidget()
    inspector.attach_host_layout(live_host, QVBoxLayout(live_host))
    app.processEvents()
    assert len(inspector._bars) <= 2
    live_host.close()


def test_catalog_and_detail_remain_interactive_inside_modal_host(
    inspector: DeveloperInspectorController,
) -> None:
    app = _app()
    host = QDialog()
    layout = QVBoxLayout(host)
    target = QPushButton("설정 안의 요소", host)
    layout.addWidget(target)
    metadata = _metadata(
        "SETTINGS-MODAL-TARGET",
        name="설정 창 테스트 요소",
        screen="설정 > 장비·자격 증명",
        source="src/aruba2930f_backup/gui.py",
    )
    inspector.register_widget(target, metadata)
    bar = inspector.attach_host_layout(host, layout)
    errors: list[BaseException] = []

    def exercise_modal_tools() -> None:
        try:
            QTest.keyClick(target, Qt.Key_F12)
            QTest.mouseClick(bar.catalog_button, Qt.LeftButton)
            app.processEvents()
            catalog = inspector.catalog_dialog
            assert catalog is not None
            assert catalog.parentWidget() is host
            assert catalog.isVisible()
            for row in range(catalog.element_list.count()):
                item = catalog.element_list.item(row)
                if item.data(Qt.UserRole) == metadata.stable_id:
                    catalog.element_list.setCurrentRow(row)
                    break
            QTest.mouseClick(catalog.details_button, Qt.LeftButton)
            app.processEvents()
            detail = inspector.detail_dialog
            assert detail is not None
            assert detail.parentWidget() is host
            assert detail.isVisible()
            QTest.mouseClick(detail.copy_button, Qt.LeftButton)
            assert metadata.stable_id in QApplication.clipboard().text()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            host.accept()

    QTimer.singleShot(0, exercise_modal_tools)
    assert host.exec() == QDialog.Accepted
    if errors:
        raise errors[0]


def test_escape_cancels_selection_without_disabling_or_clicking(
    inspector: DeveloperInspectorController,
) -> None:
    app = _app()
    button = QPushButton("실제 동작")
    inspector.register_widget(button, _metadata())
    clicked_spy = QSignalSpy(button.clicked)
    button.show()
    app.processEvents()

    QTest.keyClick(button, Qt.Key_F12)
    assert inspector.begin_selection()
    QTest.keyClick(button, Qt.Key_Escape)
    assert inspector.selection_mode is False
    assert inspector.enabled is True
    assert clicked_spy.count() == 0

    QTest.mouseClick(button, Qt.LeftButton)
    assert clicked_spy.count() == 1
    button.close()


def test_table_viewport_header_and_tab_bar_can_be_registered_separately(
    inspector: DeveloperInspectorController,
) -> None:
    app = _app()
    host = QWidget()
    layout = QVBoxLayout(host)
    table = QTableWidget(1, 2, host)
    table.setHorizontalHeaderLabels(["장비", "상태"])
    table.setItem(0, 0, QTableWidgetItem("민감한 장비 별칭"))
    table.setItem(0, 1, QTableWidgetItem("민감한 상태"))
    tabs = QTabWidget(host)
    tabs.addTab(QWidget(), "첫 탭")
    tabs.addTab(QWidget(), "둘째 탭")
    layout.addWidget(table)
    layout.addWidget(tabs)

    table_meta = _metadata("MAIN-FULL-DEVICE-TABLE", name="전체 보기 장비표")
    viewport_meta = _metadata(
        "MAIN-FULL-DEVICE-TABLE-BODY",
        name="전체 보기 장비표 본문",
    )
    header_meta = _metadata(
        "MAIN-FULL-DEVICE-TABLE-HEADER",
        name="전체 보기 장비표 머리글",
    )
    tab_meta = _metadata(
        "MAIN-VIEW-TABS",
        name="보기 전환 탭",
    )
    inspector.register_widget(table, table_meta)
    inspector.register_widget(table.viewport(), viewport_meta)
    inspector.register_widget(table.horizontalHeader(), header_meta)
    inspector.register_widget(tabs.tabBar(), tab_meta)
    selected_spy = QSignalSpy(inspector.element_selected)
    cell_spy = QSignalSpy(table.cellClicked)
    header_spy = QSignalSpy(table.horizontalHeader().sectionClicked)
    host.resize(600, 420)
    host.show()
    app.processEvents()

    QTest.keyClick(table, Qt.Key_F12)
    assert inspector.begin_selection()
    cell_rect = table.visualItemRect(table.item(0, 0))
    QTest.mouseClick(table.viewport(), Qt.LeftButton, pos=cell_rect.center())
    assert selected_spy.at(0)[0] == viewport_meta
    assert cell_spy.count() == 0

    assert inspector.begin_selection()
    header = table.horizontalHeader()
    header_point = header.rect().center()
    QTest.mouseClick(header, Qt.LeftButton, pos=header_point)
    assert selected_spy.at(1)[0] == header_meta
    assert header_spy.count() == 0

    assert tabs.currentIndex() == 0
    assert inspector.begin_selection()
    second_tab = tabs.tabBar().tabRect(1).center()
    QTest.mouseClick(tabs.tabBar(), Qt.LeftButton, pos=second_tab)
    assert selected_spy.at(2)[0] == tab_meta
    assert tabs.currentIndex() == 0
    host.close()


def test_registered_menu_action_is_selected_without_triggering_action(
    inspector: DeveloperInspectorController,
) -> None:
    app = _app()
    host = QWidget()
    menu = QMenu(host)
    action = menu.addAction("192.0.2.20 원본 출력 복사")
    menu_meta = _metadata("MAIN-MORE-MENU", name="더보기 메뉴")
    action_meta = _metadata(
        "MAIN-MORE-COPY-RAW-ACTION",
        name="원본 출력 복사 메뉴 항목",
    )
    inspector.register_menu(menu, menu_meta)
    inspector.register_action(action, action_meta)
    selected_spy = QSignalSpy(inspector.element_selected)
    triggered_spy = QSignalSpy(action.triggered)
    host.show()
    app.processEvents()

    QTest.keyClick(host, Qt.Key_F12)
    menu.popup(host.mapToGlobal(host.rect().center()))
    app.processEvents()
    assert inspector.begin_selection()
    QTest.mouseClick(menu, Qt.LeftButton, pos=menu.actionGeometry(action).center())
    app.processEvents()

    assert selected_spy.count() == 1
    assert selected_spy.at(0)[0] == action_meta
    assert triggered_spy.count() == 0
    menu.hide()
    host.close()


def test_catalog_deduplicates_shared_metadata_and_rejects_id_redefinition(
    inspector: DeveloperInspectorController,
) -> None:
    widget = QWidget()
    action = QAction(widget)
    metadata = _metadata()
    inspector.register_widget(widget, metadata)
    inspector.register_action(action, metadata)
    assert inspector.catalog == (metadata,)
    assert widget.property("uiInspectorId") == metadata.stable_id
    assert action.property("uiInspectorId") == metadata.stable_id
    assert widget.objectName() == ""

    conflicting = _metadata(name="같은 ID의 다른 이름")
    with pytest.raises(ValueError, match="different metadata"):
        inspector.register_widget(QWidget(), conflicting)
    widget.close()


def test_selection_consumes_context_menu_and_ignores_inspector_internal_widgets(
    inspector: DeveloperInspectorController,
) -> None:
    app = _app()
    host = QWidget()
    layout = QVBoxLayout(host)
    button = QPushButton("원래 컨텍스트 메뉴", host)
    button.setContextMenuPolicy(Qt.CustomContextMenu)
    layout.addWidget(button)
    metadata = _metadata("MAIN-CONTEXT-TARGET", name="컨텍스트 메뉴 대상")
    inspector.register_widget(host, metadata)
    bar = inspector.attach_host_layout(host, layout)
    context_spy = QSignalSpy(button.customContextMenuRequested)
    host.show()
    app.processEvents()

    QTest.keyClick(button, Qt.Key_F12)
    assert inspector.begin_selection()
    event = QContextMenuEvent(
        QContextMenuEvent.Mouse,
        button.rect().center(),
        button.mapToGlobal(button.rect().center()),
    )
    QApplication.sendEvent(button, event)
    app.processEvents()
    assert event.isAccepted()
    assert context_spy.count() == 0
    assert inspector.selection_mode is True

    QTest.mouseMove(bar.select_button, bar.select_button.rect().center())
    app.processEvents()
    assert inspector.hovered_metadata is None
    QTest.mouseClick(bar.select_button, Qt.LeftButton)
    assert inspector.selection_mode is True

    QTest.keyClick(button, Qt.Key_Escape)
    button.close()
    host.close()


def test_request_copy_uses_only_fixed_metadata_and_leaves_user_fields_blank(
    inspector: DeveloperInspectorController,
) -> None:
    app = _app()
    runtime_widget = QPushButton(
        "password=do-not-copy 192.0.2.33 customer-controller",
    )
    metadata = _metadata(
        "SETTINGS-CONNECTION-TEST",
        name="연결 테스트 버튼",
        screen="설정 > 연결",
        source="src/aruba2930f_backup/gui.py",
        purpose="입력한 연결 설정을 저장 전에 확인합니다.",
    )
    inspector.register_widget(runtime_widget, metadata)

    expected = (
        "프로그램 버전: v0.1.7\n"
        "화면 위치: 설정 > 연결\n"
        "요소 이름: 연결 테스트 버튼\n"
        "UI 식별자: SETTINGS-CONNECTION-TEST\n"
        "소스 위치: src/aruba2930f_backup/gui.py\n"
        "용도: 입력한 연결 설정을 저장 전에 확인합니다.\n\n"
        "현재 현상:\n"
        "원하는 변경:\n"
    )
    assert inspector.request_text(metadata) == expected
    assert build_static_request_text(metadata, "v0.1.7") == expected
    assert "password" not in expected
    assert "192.0.2.33" not in expected
    assert metadata.source_path in expected
    assert metadata.purpose in expected

    runtime_widget.show()
    app.processEvents()
    QTest.keyClick(runtime_widget, Qt.Key_F12)
    dialog = inspector.show_element_detail(metadata)
    assert dialog is not None
    copied = dialog.copy_request()
    assert copied == expected
    assert QApplication.clipboard().text() == expected
    assert dialog.source_value.text() == metadata.source_path
    assert dialog.purpose_value.toPlainText() == metadata.purpose
    runtime_widget.close()
