"""Opt-in, process-local UI element inspector for developer handoff.

Adapted from aruba-mm-wlc-mini-dashboard commit 66428acc under the MIT License.

The inspector deliberately has no configuration, command-line, environment,
registry, or storage integration.  A newly constructed controller is always
disabled and an unmodified F12 key press is its only activation path.
"""

from __future__ import annotations

import re
import weakref
from collections import OrderedDict
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from time import monotonic
from typing import Any, cast

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QKeyEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

_STABLE_ID_PATTERN = re.compile(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*\Z")
_VERSION_PATTERN = re.compile(r"v?\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?\Z")
_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_DEFAULT_MINIMUM_SIZE = QSize(320, 240)


def _bounded_window_geometry(
    preferred: QRect,
    available: QRect,
    *,
    minimum_size: QSize = _DEFAULT_MINIMUM_SIZE,
    margin: int = 16,
) -> QRect:
    """Return a usable window rectangle contained by one available screen."""

    if available.isEmpty():
        return QRect(preferred)
    safe_margin = max(0, int(margin))
    horizontal_margin = min(safe_margin, max(0, (available.width() - 1) // 2))
    vertical_margin = min(safe_margin, max(0, (available.height() - 1) // 2))
    usable = available.adjusted(
        horizontal_margin,
        vertical_margin,
        -horizontal_margin,
        -vertical_margin,
    )
    if usable.isEmpty():
        usable = QRect(available)

    minimum_width = min(max(1, minimum_size.width()), usable.width())
    minimum_height = min(max(1, minimum_size.height()), usable.height())
    width = min(max(preferred.width(), minimum_width), usable.width())
    height = min(max(preferred.height(), minimum_height), usable.height())
    maximum_x = usable.left() + usable.width() - width
    maximum_y = usable.top() + usable.height() - height
    x = min(max(preferred.x(), usable.left()), maximum_x)
    y = min(max(preferred.y(), usable.top()), maximum_y)
    return QRect(QPoint(x, y), QSize(width, height))


def _available_screen_geometry(
    widget: QWidget,
    preferred: QRect | None = None,
) -> QRect:
    """Choose the available monitor containing the proposed dialog geometry."""

    screens = QApplication.screens()
    if preferred is not None and not preferred.isEmpty() and screens:
        intersections = []
        for screen in screens:
            intersection = screen.availableGeometry().intersected(preferred)
            area = 0 if intersection.isEmpty() else intersection.width() * intersection.height()
            intersections.append((area, screen))
        area, screen = max(intersections, key=lambda item: item[0])
        if area > 0:
            return screen.availableGeometry()

    parent = widget.parentWidget()
    if parent is not None:
        parent_screen = parent.screen()
        if parent_screen is not None:
            return parent_screen.availableGeometry()
    screen = widget.screen() or QApplication.primaryScreen()
    return screen.availableGeometry() if screen is not None else QRect(0, 0, 800, 600)


def _fit_window_to_available_screen(
    widget: QWidget,
    preferred_size: QSize,
    *,
    preferred_position: QPoint | None = None,
    minimum_size: QSize = _DEFAULT_MINIMUM_SIZE,
    margin: int = 16,
    center_on_parent: bool = False,
) -> QRect:
    """Apply a DPI- and multi-monitor-safe initial dialog geometry."""

    parent = widget.parentWidget()
    provisional = QRect(preferred_position or widget.pos(), preferred_size)
    available = _available_screen_geometry(
        widget,
        provisional if preferred_position else None,
    )
    if preferred_position is None and center_on_parent:
        center = (
            parent.frameGeometry().center()
            if parent is not None and parent.isVisible()
            else available.center()
        )
        provisional.moveCenter(center)
    bounded = _bounded_window_geometry(
        provisional,
        available,
        minimum_size=minimum_size,
        margin=margin,
    )
    widget.setMinimumSize(
        min(minimum_size.width(), bounded.width()),
        min(minimum_size.height(), bounded.height()),
    )
    widget.setGeometry(bounded)
    return bounded


def _static_field(value: str, field_name: str, *, maximum: int = 500) -> str:
    """Validate hard-coded catalog text without silently rewriting it."""

    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be non-empty and trimmed")
    if len(value) > maximum:
        raise ValueError(f"{field_name} is too long")
    if _CONTROL_CHAR_PATTERN.search(value):
        raise ValueError(f"{field_name} must be a single printable line")
    return value


def _repository_relative_source(value: str) -> str:
    source = _static_field(value, "source_path", maximum=260)
    if "\\" in source or ":" in source:
        raise ValueError("source_path must use repository-relative POSIX separators")
    posix_path = PurePosixPath(source)
    windows_path = PureWindowsPath(source)
    if (
        source != posix_path.as_posix()
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or any(part in {"", ".", ".."} for part in posix_path.parts)
    ):
        raise ValueError("source_path must be a repository-relative path")
    return source


def _application_version(value: str) -> str:
    version = _static_field(value, "app_version", maximum=64)
    if not _VERSION_PATTERN.fullmatch(version):
        raise ValueError("app_version must be a static dotted version string")
    return version


@dataclass(frozen=True, slots=True)
class UiElementMetadata:
    """Fixed, non-runtime metadata for one user-visible UI element."""

    name_ko: str
    stable_id: str
    screen_path: str
    source_path: str
    purpose: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name_ko", _static_field(self.name_ko, "name_ko"))
        stable_id = _static_field(self.stable_id, "stable_id", maximum=128)
        if not _STABLE_ID_PATTERN.fullmatch(stable_id):
            raise ValueError("stable_id must use uppercase ASCII words separated by hyphens")
        object.__setattr__(self, "stable_id", stable_id)
        object.__setattr__(
            self,
            "screen_path",
            _static_field(self.screen_path, "screen_path"),
        )
        object.__setattr__(
            self,
            "source_path",
            _repository_relative_source(self.source_path),
        )
        object.__setattr__(self, "purpose", _static_field(self.purpose, "purpose"))


def build_static_request_text(
    metadata: UiElementMetadata,
    app_version: str,
) -> str:
    """Build a sanitized handoff template solely from fixed catalog metadata."""

    version = _application_version(app_version)
    return (
        f"프로그램 버전: {version}\n"
        f"화면 위치: {metadata.screen_path}\n"
        f"요소 이름: {metadata.name_ko}\n"
        f"UI 식별자: {metadata.stable_id}\n"
        f"소스 위치: {metadata.source_path}\n"
        f"용도: {metadata.purpose}\n\n"
        "현재 현상:\n"
        "원하는 변경:\n"
    )


class DeveloperInspectorBar(QFrame):
    """Visible-only-while-enabled host bar with no activation control."""

    def __init__(
        self,
        controller: DeveloperInspectorController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("uiInspectorInternal", True)
        self.setObjectName("developerInspectorBar")
        self.setFrameShape(QFrame.Shape.StyledPanel)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)
        self.mode_label = QLabel("개발자 UI 식별 모드", self)
        self.mode_label.setObjectName("developerInspectorModeLabel")
        layout.addWidget(self.mode_label)
        layout.addStretch(1)

        self.select_button = QPushButton("요소 선택", self)
        self.catalog_button = QPushButton("요소 목록", self)
        self.exit_button = QPushButton("종료", self)
        layout.addWidget(self.select_button)
        layout.addWidget(self.catalog_button)
        layout.addWidget(self.exit_button)

        self.select_button.clicked.connect(controller.begin_selection)
        self.catalog_button.clicked.connect(lambda: controller.show_catalog(self.window()))
        self.exit_button.clicked.connect(controller.deactivate)
        controller.enabled_changed.connect(self._sync_enabled)
        controller.selection_mode_changed.connect(self._sync_selection_mode)
        self._sync_selection_mode(False)
        self._sync_enabled(controller.enabled)

    def _sync_enabled(self, enabled: bool) -> None:
        self.setVisible(enabled)

    def _sync_selection_mode(self, selecting: bool) -> None:
        self.select_button.setText("선택 중 (Esc로 취소)" if selecting else "요소 선택")
        self.select_button.setChecked(selecting)


class DeveloperInspectorDetailDialog(QDialog):
    """Nonmodal view of fixed catalog metadata and the sanitized request text."""

    def __init__(self, app_version: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("uiInspectorInternal", True)
        self._app_version = _application_version(app_version)
        self._metadata: UiElementMetadata | None = None
        self.setWindowTitle("UI 요소 정보")
        self.setModal(False)

        root = QVBoxLayout(self)
        form = QFormLayout()
        self.name_value = self._read_only_line()
        self.id_value = self._read_only_line()
        self.screen_value = self._read_only_line()
        self.source_value = self._read_only_line()
        self.purpose_value = QTextEdit(self)
        self.purpose_value.setReadOnly(True)
        self.purpose_value.setAcceptRichText(False)
        self.purpose_value.setMaximumHeight(90)
        form.addRow("사용자용 이름", self.name_value)
        form.addRow("고정 식별자", self.id_value)
        form.addRow("화면 위치", self.screen_value)
        form.addRow("소스 위치", self.source_value)
        form.addRow("기능", self.purpose_value)
        root.addLayout(form)

        self.request_preview = QTextEdit(self)
        self.request_preview.setReadOnly(True)
        self.request_preview.setAcceptRichText(False)
        root.addWidget(self.request_preview)

        buttons = QDialogButtonBox(self)
        self.copy_button = buttons.addButton(
            "작업 요청 복사",
            QDialogButtonBox.ButtonRole.ActionRole,
        )
        close_button = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        self.copy_button.clicked.connect(self.copy_request)
        close_button.clicked.connect(self.hide)
        root.addWidget(buttons)
        _fit_window_to_available_screen(
            self,
            QSize(560, 430),
            minimum_size=QSize(320, 300),
            center_on_parent=True,
        )

    def _read_only_line(self) -> QLineEdit:
        line = QLineEdit(self)
        line.setReadOnly(True)
        return line

    @property
    def metadata(self) -> UiElementMetadata | None:
        return self._metadata

    def set_metadata(self, metadata: UiElementMetadata) -> None:
        self._metadata = metadata
        self.name_value.setText(metadata.name_ko)
        self.id_value.setText(metadata.stable_id)
        self.screen_value.setText(metadata.screen_path)
        self.source_value.setText(metadata.source_path)
        self.purpose_value.setPlainText(metadata.purpose)
        self.request_preview.setPlainText(build_static_request_text(metadata, self._app_version))

    def copy_request(self) -> str:
        if self._metadata is None:
            return ""
        text = build_static_request_text(self._metadata, self._app_version)
        QApplication.clipboard().setText(text)
        return text


class DeveloperInspectorCatalogDialog(QDialog):
    """Nonmodal fixed-metadata catalog."""

    metadata_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("uiInspectorInternal", True)
        self.setWindowTitle("UI 요소 목록")
        self.setModal(False)
        self._metadata_by_id: dict[str, UiElementMetadata] = {}

        root = QVBoxLayout(self)
        self.element_list = QListWidget(self)
        self.element_list.itemDoubleClicked.connect(self._request_item)
        root.addWidget(self.element_list)

        buttons = QDialogButtonBox(self)
        self.details_button = buttons.addButton(
            "상세 보기",
            QDialogButtonBox.ButtonRole.ActionRole,
        )
        self.close_button = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        self.details_button.clicked.connect(self._request_current)
        self.close_button.clicked.connect(self.hide)
        root.addWidget(buttons)
        _fit_window_to_available_screen(
            self,
            QSize(590, 420),
            minimum_size=QSize(320, 240),
            center_on_parent=True,
        )

    def set_catalog(self, metadata_items: Iterable[UiElementMetadata]) -> None:
        selected_id = None
        if self.element_list.currentItem() is not None:
            selected_id = self.element_list.currentItem().data(Qt.ItemDataRole.UserRole)
        self.element_list.clear()
        self._metadata_by_id.clear()
        for metadata in metadata_items:
            self._metadata_by_id[metadata.stable_id] = metadata
            item = QListWidgetItem(
                f"{metadata.name_ko}  ({metadata.stable_id})",
                self.element_list,
            )
            item.setData(Qt.ItemDataRole.UserRole, metadata.stable_id)
            if metadata.stable_id == selected_id:
                self.element_list.setCurrentItem(item)
        if self.element_list.currentRow() < 0 and self.element_list.count():
            self.element_list.setCurrentRow(0)

    def _request_current(self) -> None:
        item = self.element_list.currentItem()
        if item is not None:
            self._request_item(item)

    def _request_item(self, item: QListWidgetItem) -> None:
        metadata = self._metadata_by_id.get(item.data(Qt.ItemDataRole.UserRole))
        if metadata is not None:
            self.metadata_requested.emit(metadata)


class _SelectionOutline(QWidget):
    """Mouse-transparent outline positioned over the current registered hit."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setProperty("uiInspectorInternal", True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

    def show_for_widget(self, widget: QWidget) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        top_left = widget.mapTo(parent, QPoint(0, 0))
        self._show_rect(QRect(top_left, widget.size()))

    def show_for_rect(self, widget: QWidget, rect: QRect) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        top_left = widget.mapTo(parent, rect.topLeft())
        self._show_rect(QRect(top_left, rect.size()))

    def _show_rect(self, rect: QRect) -> None:
        self.setGeometry(rect)
        self.show()
        self.raise_()
        self.update()

    def paintEvent(self, event: Any) -> None:
        del event
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#d96c00"), 3))
            painter.drawRect(self.rect().adjusted(1, 1, -2, -2))
        finally:
            painter.end()


@dataclass(frozen=True, slots=True)
class _RegisteredTarget:
    reference: weakref.ReferenceType[QObject]
    metadata: UiElementMetadata


@dataclass(frozen=True, slots=True)
class _Hit:
    metadata: UiElementMetadata
    widget: QWidget
    rect: QRect | None = None


class DeveloperInspectorController(QObject):
    """Application-wide developer inspector with an F12-only activation gate."""

    enabled_changed = Signal(bool)
    selection_mode_changed = Signal(bool)
    element_selected = Signal(object)

    _HOVER_EVENTS = frozenset(
        {
            QEvent.Type.Enter,
            QEvent.Type.HoverEnter,
            QEvent.Type.HoverMove,
            QEvent.Type.MouseMove,
        }
    )
    _MOUSE_EVENTS = frozenset(
        {
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseButtonDblClick,
        }
    )

    def __init__(
        self,
        app: QApplication,
        app_version: str,
        parent: QObject | None = None,
    ) -> None:
        if not isinstance(app, QApplication):
            raise TypeError("app must be QApplication")
        super().__init__(parent)
        self._app = app
        self._app_version = _application_version(app_version)
        self._enabled = False
        self._selection_mode = False
        self._closed = False
        self._catalog: OrderedDict[str, UiElementMetadata] = OrderedDict()
        self._targets: dict[int, _RegisteredTarget] = {}
        self._bars: list[weakref.ReferenceType[DeveloperInspectorBar]] = []
        self._tracking_states: dict[
            int,
            tuple[weakref.ReferenceType[QWidget], bool, bool],
        ] = {}
        self._suppressed_mouse_buttons: set[Qt.MouseButton] = set()
        self._post_selection_guard: (
            tuple[
                weakref.ReferenceType[QObject],
                Qt.MouseButton,
                float,
            ]
            | None
        ) = None
        self._suppress_escape_release = False
        self._hovered_metadata: UiElementMetadata | None = None
        self._outline: _SelectionOutline | None = None
        self._detail_dialog: DeveloperInspectorDetailDialog | None = None
        self._catalog_dialog: DeveloperInspectorCatalogDialog | None = None
        self._app.installEventFilter(self)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def selection_mode(self) -> bool:
        return self._selection_mode

    @property
    def hovered_metadata(self) -> UiElementMetadata | None:
        return self._hovered_metadata

    @property
    def catalog(self) -> tuple[UiElementMetadata, ...]:
        return tuple(self._catalog.values())

    @property
    def detail_dialog(self) -> DeveloperInspectorDetailDialog | None:
        return self._detail_dialog

    @property
    def catalog_dialog(self) -> DeveloperInspectorCatalogDialog | None:
        return self._catalog_dialog

    def register_widget(
        self,
        widget: QWidget,
        metadata: UiElementMetadata,
    ) -> None:
        if not isinstance(widget, QWidget):
            raise TypeError("widget must be QWidget")
        self._register_target(widget, metadata)
        if self._selection_mode:
            self._enable_tracking_tree(widget)

    def register_menu(self, menu: QMenu, metadata: UiElementMetadata) -> None:
        if not isinstance(menu, QMenu):
            raise TypeError("menu must be QMenu")
        self.register_widget(menu, metadata)

    def register_action(
        self,
        action: QAction,
        metadata: UiElementMetadata,
    ) -> None:
        if not isinstance(action, QAction):
            raise TypeError("action must be QAction")
        self._register_target(action, metadata)

    def _register_target(
        self,
        target: QObject,
        metadata: UiElementMetadata,
    ) -> None:
        if not isinstance(metadata, UiElementMetadata):
            raise TypeError("metadata must be UiElementMetadata")
        existing_metadata = self._catalog.get(metadata.stable_id)
        if existing_metadata is not None and existing_metadata != metadata:
            raise ValueError(f"stable_id {metadata.stable_id!r} already has different metadata")
        key = id(target)
        existing_target = self._targets.get(key)
        if existing_target is not None:
            existing_object = existing_target.reference()
            if existing_object is target and existing_target.metadata != metadata:
                raise ValueError("target is already registered with different metadata")
        self._catalog.setdefault(metadata.stable_id, metadata)
        target_reference = weakref.ref(target)
        self._targets[key] = _RegisteredTarget(target_reference, metadata)
        target.setProperty("uiInspectorId", metadata.stable_id)
        controller_ref = weakref.ref(self)

        def remove_destroyed_target(*_args: object, target_key: int = key) -> None:
            controller = controller_ref()
            targets = getattr(controller, "_targets", None)
            if targets is not None:
                registered = targets.get(target_key)
                if registered is not None and registered.reference is target_reference:
                    targets.pop(target_key, None)

        target.destroyed.connect(remove_destroyed_target)

    def attach_host_layout(
        self,
        host: QWidget,
        layout: QVBoxLayout,
    ) -> DeveloperInspectorBar:
        if not isinstance(host, QWidget):
            raise TypeError("host must be QWidget")
        if not isinstance(layout, QVBoxLayout):
            raise TypeError("layout must be QVBoxLayout")
        self._bars = [reference for reference in self._bars if reference() is not None]
        bar = DeveloperInspectorBar(self, host)
        layout.insertWidget(0, bar)
        self._bars.append(weakref.ref(bar))
        return bar

    def begin_selection(self) -> bool:
        """Enter element selection only after F12 has enabled the inspector."""

        if not self._enabled or self._selection_mode:
            return False
        if self._detail_dialog is not None:
            self._detail_dialog.hide()
        if self._catalog_dialog is not None:
            self._catalog_dialog.hide()
        self._selection_mode = True
        self._hovered_metadata = None
        self._enable_registered_mouse_tracking()
        QApplication.setOverrideCursor(Qt.CursorShape.CrossCursor)
        self.selection_mode_changed.emit(True)
        return True

    def cancel_selection(self) -> bool:
        if not self._selection_mode:
            return False
        self._selection_mode = False
        self._hovered_metadata = None
        self._hide_outline()
        self._restore_mouse_tracking()
        QApplication.restoreOverrideCursor()
        self.selection_mode_changed.emit(False)
        return True

    def deactivate(self) -> None:
        """Disable the inspector; this method can never activate it."""

        self.cancel_selection()
        self._hide_outline()
        if self._detail_dialog is not None:
            try:
                self._detail_dialog.hide()
            except RuntimeError:
                self._detail_dialog = None
        if self._catalog_dialog is not None:
            try:
                self._catalog_dialog.hide()
            except RuntimeError:
                self._catalog_dialog = None
        if not self._enabled:
            return
        self._enabled = False
        self.enabled_changed.emit(False)

    def show_catalog(
        self,
        owner: QWidget | None = None,
    ) -> DeveloperInspectorCatalogDialog | None:
        if not self._enabled:
            return None
        owner = self._dialog_owner(owner)
        if not self._dialog_matches_owner(self._catalog_dialog, owner):
            if self._catalog_dialog is not None:
                self._dispose_dialog(self._catalog_dialog)
            self._catalog_dialog = DeveloperInspectorCatalogDialog(owner)
            self._catalog_dialog.metadata_requested.connect(self.show_element_detail)
            self._clear_dialog_reference_on_destroy(
                self._catalog_dialog,
                "_catalog_dialog",
            )
        dialog = self._catalog_dialog
        if dialog is None:  # Defensive guard for deleted Qt wrappers.
            return None
        dialog.set_catalog(self.catalog)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def show_element_detail(
        self,
        metadata: UiElementMetadata,
        owner: QWidget | None = None,
    ) -> DeveloperInspectorDetailDialog | None:
        if not self._enabled:
            return None
        if metadata.stable_id not in self._catalog:
            raise ValueError("metadata is not registered in this inspector catalog")
        owner = self._dialog_owner(owner)
        if not self._dialog_matches_owner(self._detail_dialog, owner):
            if self._detail_dialog is not None:
                self._dispose_dialog(self._detail_dialog)
            self._detail_dialog = DeveloperInspectorDetailDialog(
                self._app_version,
                owner,
            )
            self._clear_dialog_reference_on_destroy(
                self._detail_dialog,
                "_detail_dialog",
            )
        dialog = self._detail_dialog
        if dialog is None:  # Defensive guard for deleted Qt wrappers.
            return None
        dialog.set_metadata(metadata)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def _dialog_owner(self, owner: QWidget | None) -> QWidget | None:
        if owner is not None and not bool(owner.property("uiInspectorInternal")):
            return owner.window()
        modal = self._app.activeModalWidget()
        if modal is not None and not bool(modal.property("uiInspectorInternal")):
            return modal.window()
        active = self._app.activeWindow()
        if active is not None and not bool(active.property("uiInspectorInternal")):
            return active.window()
        return None

    @staticmethod
    def _dialog_matches_owner(dialog: QDialog | None, owner: QWidget | None) -> bool:
        if dialog is None:
            return False
        try:
            return dialog.parentWidget() is owner
        except RuntimeError:
            return False

    @staticmethod
    def _dispose_dialog(dialog: QDialog) -> None:
        try:
            dialog.hide()
            dialog.deleteLater()
        except RuntimeError:
            pass

    def _clear_dialog_reference_on_destroy(
        self,
        dialog: QDialog,
        attribute_name: str,
    ) -> None:
        controller_ref = weakref.ref(self)
        dialog_ref = weakref.ref(dialog)

        def clear_reference(*_args: object) -> None:
            controller = controller_ref()
            if controller is None:
                return
            if getattr(controller, attribute_name, None) is dialog_ref():
                setattr(controller, attribute_name, None)

        dialog.destroyed.connect(clear_reference)

    def request_text(self, metadata: UiElementMetadata) -> str:
        if self._catalog.get(metadata.stable_id) != metadata:
            raise ValueError("metadata is not registered in this inspector catalog")
        return build_static_request_text(metadata, self._app_version)

    def close(self) -> None:
        """Remove the app event filter and release process-local UI state."""

        if self._closed:
            return
        self._closed = True
        with suppress(RuntimeError):
            self._app.removeEventFilter(self)
        self.deactivate()
        outline = self._outline
        self._outline = None
        if outline is not None:
            try:
                outline.hide()
                outline.deleteLater()
            except RuntimeError:
                pass
        if self._detail_dialog is not None:
            try:
                self._detail_dialog.hide()
                self._detail_dialog.deleteLater()
            except RuntimeError:
                pass
        if self._catalog_dialog is not None:
            try:
                self._catalog_dialog.hide()
                self._catalog_dialog.deleteLater()
            except RuntimeError:
                pass
        self._suppressed_mouse_buttons.clear()
        self._post_selection_guard = None
        self._bars.clear()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if getattr(self, "_closed", True):
            return False
        event_type = event.type()

        if event_type in {
            QEvent.Type.ShortcutOverride,
            QEvent.Type.KeyPress,
            QEvent.Type.KeyRelease,
        } and isinstance(event, QKeyEvent):
            if self._is_plain_f12(event):
                event.accept()
                if event_type == QEvent.Type.KeyPress and not event.isAutoRepeat():
                    self._set_enabled_from_f12(not self._enabled)
                return True

            if self._selection_mode and event.key() == Qt.Key.Key_Escape:
                event.accept()
                if event_type == QEvent.Type.KeyPress and not event.isAutoRepeat():
                    self._suppress_escape_release = True
                    self.cancel_selection()
                return True

            if (
                self._suppress_escape_release
                and event.key() == Qt.Key.Key_Escape
                and event_type == QEvent.Type.KeyRelease
            ):
                self._suppress_escape_release = False
                event.accept()
                return True

        if event_type in self._MOUSE_EVENTS and isinstance(event, QMouseEvent):
            if self._is_inspector_internal(watched):
                return False
            button = event.button()
            self._expire_post_selection_guard()
            if event_type == QEvent.Type.MouseButtonPress:
                # A release belonging to an inspector-consumed press can be
                # lost when focus/window ownership changes.  Do not let that
                # stale guard swallow the release of the next real click.
                self._suppressed_mouse_buttons.discard(button)
                self._post_selection_guard = None
            if (
                event_type == QEvent.Type.MouseButtonRelease
                and button in self._suppressed_mouse_buttons
            ):
                self._suppressed_mouse_buttons.discard(button)
                event.accept()
                return True
            if event_type == QEvent.Type.MouseButtonDblClick and self._post_selection_matches(
                watched, button
            ):
                self._post_selection_guard = None
                self._suppressed_mouse_buttons.add(button)
                event.accept()
                return True
            if self._selection_mode:
                event.accept()
                if event_type in {
                    QEvent.Type.MouseButtonPress,
                    QEvent.Type.MouseButtonDblClick,
                }:
                    self._suppressed_mouse_buttons.add(button)
                    if button == Qt.MouseButton.LeftButton:
                        hit = self._hit_for_event(watched, event)
                        if hit is not None:
                            self._select_hit(hit)
                            self._arm_post_selection_guard(watched, button)
                return True
            if (
                event_type == QEvent.Type.MouseButtonDblClick
                and button in self._suppressed_mouse_buttons
            ):
                event.accept()
                return True

        if self._selection_mode and event_type == QEvent.Type.ContextMenu:
            event.accept()
            return True

        if self._selection_mode and event_type in self._HOVER_EVENTS:
            hit = self._hit_for_event(watched, event)
            self._show_hit(hit)

        return super().eventFilter(watched, event)

    @staticmethod
    def _is_inspector_internal(watched: QObject) -> bool:
        current: QObject | None = watched
        while current is not None:
            try:
                if bool(current.property("uiInspectorInternal")):
                    return True
            except RuntimeError:
                return False
            current = current.parent()
        return False

    def _arm_post_selection_guard(
        self,
        watched: QObject,
        button: Qt.MouseButton,
    ) -> None:
        self._post_selection_guard = (
            weakref.ref(watched),
            button,
            monotonic() + (self._app.doubleClickInterval() / 1000.0),
        )

    def _expire_post_selection_guard(self) -> None:
        guard = self._post_selection_guard
        if guard is not None and monotonic() > guard[2]:
            self._post_selection_guard = None

    def _post_selection_matches(
        self,
        watched: QObject,
        button: Qt.MouseButton,
    ) -> bool:
        guard = self._post_selection_guard
        if guard is None:
            return False
        return guard[0]() is watched and guard[1] == button

    @staticmethod
    def _is_plain_f12(event: QKeyEvent) -> bool:
        return bool(
            event.key() == Qt.Key.Key_F12 and event.modifiers() == Qt.KeyboardModifier.NoModifier
        )

    def _set_enabled_from_f12(self, enabled: bool) -> None:
        if enabled == self._enabled:
            return
        if not enabled:
            self.deactivate()
            return
        self._enabled = True
        self.enabled_changed.emit(True)

    def _target_metadata(self, target: QObject) -> UiElementMetadata | None:
        registered = self._targets.get(id(target))
        if registered is None or registered.reference() is not target:
            return None
        return registered.metadata

    def _hit_for_event(self, watched: QObject, event: QEvent) -> _Hit | None:
        global_position = self._global_position(event)
        # Qt may deliver a hover/mouse event through a registered ancestor even
        # when the pointer is over one of the inspector's own child controls.
        # Resolve the real topmost widget first so internal bars/dialogs can
        # never fall through to the host's metadata.
        widget = self._app.widgetAt(global_position) if global_position is not None else None
        if widget is None and isinstance(watched, QWidget):
            widget = watched
        if widget is None:
            return None

        internal_check: QWidget | None = widget
        while internal_check is not None:
            if bool(internal_check.property("uiInspectorInternal")):
                return None
            internal_check = internal_check.parentWidget()

        current: QWidget | None = widget
        while current is not None:
            if isinstance(current, QMenu) and global_position is not None:
                action = current.actionAt(current.mapFromGlobal(global_position))
                if action is not None:
                    metadata = self._target_metadata(action)
                    if metadata is not None:
                        return _Hit(metadata, current, current.actionGeometry(action))
            if isinstance(current, QToolButton):
                action = current.defaultAction()
                if action is not None:
                    metadata = self._target_metadata(action)
                    if metadata is not None:
                        return _Hit(metadata, current)
            metadata = self._target_metadata(current)
            if metadata is not None:
                return _Hit(metadata, current)
            current = current.parentWidget()
        return None

    @staticmethod
    def _global_position(event: QEvent) -> QPoint | None:
        global_position = getattr(event, "globalPosition", None)
        if callable(global_position):
            return cast(QPoint, global_position().toPoint())
        if event.type() in {
            QEvent.Type.Enter,
            QEvent.Type.HoverEnter,
            QEvent.Type.HoverMove,
        }:
            return QCursor.pos()
        return None

    def _show_hit(self, hit: _Hit | None) -> None:
        if hit is None:
            self._hovered_metadata = None
            self._hide_outline()
            return
        self._hovered_metadata = hit.metadata
        outline = self._outline_for(hit.widget.window())
        if hit.rect is None:
            outline.show_for_widget(hit.widget)
        else:
            outline.show_for_rect(hit.widget, hit.rect)

    def _outline_for(self, window: QWidget) -> _SelectionOutline:
        outline = self._outline
        if outline is not None:
            try:
                if outline.parentWidget() is window:
                    return outline
                outline.hide()
                outline.deleteLater()
            except RuntimeError:
                pass

        outline = _SelectionOutline(window)
        self._outline = outline
        controller_ref = weakref.ref(self)
        outline_ref = weakref.ref(outline)

        def forget_outline(*_args: object) -> None:
            controller = controller_ref()
            if controller is not None and getattr(controller, "_outline", None) is outline_ref():
                controller._outline = None

        outline.destroyed.connect(forget_outline)
        return outline

    def _hide_outline(self) -> None:
        outline = self._outline
        if outline is None:
            return
        try:
            outline.hide()
        except RuntimeError:
            self._outline = None

    def _select_hit(self, hit: _Hit) -> None:
        metadata = hit.metadata
        self.cancel_selection()
        self.element_selected.emit(metadata)
        self.show_element_detail(metadata, hit.widget.window())

    def _enable_registered_mouse_tracking(self) -> None:
        for registered in tuple(self._targets.values()):
            target = registered.reference()
            if isinstance(target, QWidget):
                self._enable_tracking_tree(target)

    def _enable_tracking_tree(self, root: QWidget) -> None:
        widgets = [root, *root.findChildren(QWidget)]
        for widget in widgets:
            key = id(widget)
            if key in self._tracking_states:
                continue
            self._tracking_states[key] = (
                weakref.ref(widget),
                widget.hasMouseTracking(),
                widget.testAttribute(Qt.WidgetAttribute.WA_Hover),
            )
            widget.setMouseTracking(True)
            widget.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def _restore_mouse_tracking(self) -> None:
        states = tuple(self._tracking_states.values())
        self._tracking_states.clear()
        for reference, had_mouse_tracking, had_hover in states:
            widget = reference()
            if widget is None:
                continue
            try:
                widget.setMouseTracking(had_mouse_tracking)
                widget.setAttribute(Qt.WidgetAttribute.WA_Hover, had_hover)
            except RuntimeError:
                # The C++ widget may have been deleted before its Python wrapper.
                continue


__all__ = [
    "DeveloperInspectorBar",
    "DeveloperInspectorCatalogDialog",
    "DeveloperInspectorController",
    "DeveloperInspectorDetailDialog",
    "UiElementMetadata",
    "build_static_request_text",
]
