"""Single-screen Qt UI: URL -> thumbnail grid (the only scrolling area) -> pick -> save to a folder."""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import (QAbstractListModel, QEvent, QModelIndex, QPoint, QRect, QRectF, QSettings, QSize, Qt,
                            QThread, QTimer, QUrl, Signal, QObject)
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QFontMetrics, QGuiApplication, QImageReader, QKeySequence,
                           QPainter, QPainterPath, QPen, QPixmap, QPixmapCache, QShortcut, QIcon)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QListView, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QSizePolicy, QSpinBox, QStyledItemDelegate, QStyleOptionViewItem,
                               QVBoxLayout, QWidget, QDialogButtonBox, QToolButton)

from . import APP_NAME, __version__, core, extract, imgutil, net

ACCENT = "#4c8dff"
BG = "#14171f"
PANEL = "#1c212c"
CARD = "#222837"
TEXT = "#e8ecf4"
MUTED = "#98a2b8"

CARD_W = {"Small": 130, "Medium": 165, "Large": 240}
TYPE_GROUPS = [("JPG", {"jpg"}), ("PNG", {"png"}), ("WEBP", {"webp"}), ("GIF", {"gif"}), ("SVG", {"svg"})]
SORTS = ["Page order", "Largest dimensions", "Smallest dimensions", "Largest file", "Smallest file"]

STYLE = f"""
* {{ font-family: "Segoe UI", "Noto Sans", "DejaVu Sans", sans-serif; font-size: 11pt; color: {TEXT}; }}
QWidget#root {{ background: {BG}; }}
QLabel {{ background: transparent; }}
QLineEdit, QSpinBox, QComboBox {{ background: {PANEL}; border: 1px solid #333b4f; border-radius: 8px; padding: 4px 10px; min-height: 34px; selection-background-color: {ACCENT}; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox QAbstractItemView {{ background: {PANEL}; border: 1px solid #333b4f; selection-background-color: {ACCENT}; min-height: 34px; }}
QPushButton, QToolButton {{ background: #2b3347; border: 1px solid #3a4560; border-radius: 8px; padding: 4px 14px; min-height: 34px; }}
QPushButton:hover, QToolButton:hover {{ background: #364060; }}
QPushButton:pressed, QToolButton:pressed {{ background: #44507a; }}
QPushButton:disabled, QToolButton:disabled {{ color: #6b7388; background: #222839; border-color: #2c3347; }}
QPushButton#primary {{ background: {ACCENT}; border-color: {ACCENT}; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: #6aa0ff; }}
QPushButton#primary:disabled {{ background: #2c3a5c; border-color: #2c3a5c; color: #8a97b6; }}
QPushButton#danger {{ background: #5b2a33; border-color: #7a3a46; }}
QToolButton:checked {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QCheckBox {{ spacing: 8px; min-height: 34px; }}
QCheckBox::indicator {{ width: 22px; height: 22px; border-radius: 5px; border: 2px solid #4a5572; background: {PANEL}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: none; }}
QProgressBar {{ background: {PANEL}; border: 1px solid #333b4f; border-radius: 7px; text-align: center; min-height: 20px; max-height: 20px; color: {TEXT}; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 6px; }}
QListView {{ background: #10131a; border: 1px solid #2a3144; border-radius: 10px; outline: none; }}
QScrollBar:vertical {{ background: #10131a; width: 22px; margin: 0; border-radius: 11px; }}
QScrollBar::handle:vertical {{ background: #3a4560; min-height: 60px; border-radius: 9px; margin: 3px; }}
QScrollBar::handle:vertical:hover {{ background: #4c5a80; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; width: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QDialog {{ background: {BG}; }}
QPlainTextEdit {{ background: {PANEL}; border: 1px solid #333b4f; border-radius: 8px; }}
"""


# ============================================================================ model

class ItemModel(QAbstractListModel):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.all: list[core.ImageItem] = []
        self.view: list[core.ImageItem] = []
        self.min_w = 0
        self.min_h = 0
        self.types: set[str] = {"JPG", "PNG", "WEBP", "GIF", "SVG", "OTHER"}
        self.include_tiny = False
        self.sort = 0
        self.dirty = False

    # --- Qt
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.view)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.view):
            return None
        if role == Qt.UserRole:
            return self.view[index.row()]
        return None

    def flags(self, index):
        return Qt.ItemIsEnabled

    # --- content
    def clear(self):
        self.beginResetModel()
        self.all, self.view = [], []
        self.endResetModel()
        self.changed.emit()

    def add(self, it: core.ImageItem):
        self.all.append(it)
        self.dirty = True

    def _type_group(self, it) -> str:
        for name, kinds in TYPE_GROUPS:
            if it.kind in kinds:
                return name
        return "OTHER"

    def passes(self, it: core.ImageItem) -> bool:
        if self._type_group(it) not in self.types:
            return False
        if it.tiny and not self.include_tiny:
            return False
        if it.w and self.min_w and it.w < self.min_w:
            return False
        if it.h and self.min_h and it.h < self.min_h:
            return False
        return True

    def refresh(self, vsb=None):
        keep = vsb.value() if vsb is not None else 0
        v = [it for it in self.all if self.passes(it)]
        s = self.sort
        if s == 0:
            v.sort(key=lambda i: i.order)
        elif s == 1:
            v.sort(key=lambda i: (-(i.w * i.h), i.order))
        elif s == 2:
            v.sort(key=lambda i: (i.w * i.h if i.w else 1 << 60, i.order))
        elif s == 3:
            v.sort(key=lambda i: (-i.size, i.order))
        else:
            v.sort(key=lambda i: (i.size, i.order))
        self.beginResetModel()
        self.view = v
        self.endResetModel()
        self.dirty = False
        if vsb is not None:
            vsb.setValue(keep)
        self.changed.emit()

    def selected_visible(self) -> list[core.ImageItem]:
        return [i for i in self.view if i.selected]

    def toggle_row(self, row: int):
        if 0 <= row < len(self.view):
            self.view[row].selected = not self.view[row].selected
            ix = self.index(row)
            self.dataChanged.emit(ix, ix)
            self.changed.emit()

    def set_selected(self, mode: str):
        for it in self.view:
            if mode == "all":
                it.selected = True
            elif mode == "none":
                it.selected = False
            else:
                it.selected = not it.selected
        if self.view:
            self.dataChanged.emit(self.index(0), self.index(len(self.view) - 1))
        self.changed.emit()


# ============================================================================ delegate

class CardDelegate(QStyledItemDelegate):
    def __init__(self, view: "GridView"):
        super().__init__(view)
        self.view = view

    def card_rect(self, cell: QRect) -> QRect:
        return cell.adjusted(5, 5, -5, -5)

    def check_rect(self, card: QRect) -> QRect:
        s = 34
        return QRect(card.left() + 8, card.top() + 8, s, s)

    def sizeHint(self, option, index):
        return self.view.cell_size()

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        it: core.ImageItem = index.data(Qt.UserRole)
        if it is None:
            return
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        card = self.card_rect(option.rect)
        path = QPainterPath()
        path.addRoundedRect(QRectF(card), 10, 10)
        p.fillPath(path, QColor(CARD))
        thumb_h = round(card.width() * 0.75)
        area = QRect(card.left() + 6, card.top() + 6, card.width() - 12, thumb_h)
        p.fillRect(area, QColor("#0d1016"))
        pm = self.view.pixmap_for(it)
        if pm is not None and not pm.isNull():
            dpr = pm.devicePixelRatio() or 1.0
            lw, lh = pm.width() / dpr, pm.height() / dpr
            k = min(area.width() / lw, area.height() / lh)
            tw, th = lw * k, lh * k
            target = QRectF(area.left() + (area.width() - tw) / 2, area.top() + (area.height() - th) / 2, tw, th)
            p.drawPixmap(target, pm, QRectF(pm.rect()))
        else:
            p.setPen(QColor(MUTED))
            f = QFont(p.font())
            f.setPointSizeF(max(8.0, f.pointSizeF() - 1))
            p.setFont(f)
            p.drawText(area, Qt.AlignCenter | Qt.TextWordWrap, f"No preview\n({it.type_text})\nwill still save")
        # text
        fm = p.fontMetrics()
        tx = card.left() + 10
        tw_ = card.width() - 20
        y = area.bottom() + 6
        f1 = QFont(p.font())
        f1.setBold(True)
        p.setFont(f1)
        p.setPen(QColor(TEXT))
        p.drawText(QRect(tx, y, tw_, fm.height() + 2), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(f1).elidedText(it.dims_text, Qt.ElideRight, tw_))
        f2 = QFont(p.font())
        f2.setBold(False)
        f2.setPointSizeF(max(8.0, f2.pointSizeF() - 1.5))
        p.setFont(f2)
        p.setPen(QColor(MUTED))
        fm2 = QFontMetrics(f2)
        y += fm.height() + 2
        p.drawText(QRect(tx, y, tw_, fm2.height() + 2), Qt.AlignLeft | Qt.AlignVCenter,
                   fm2.elidedText(f"{it.type_text} \u00b7 {core.human_size(it.size)}", Qt.ElideRight, tw_))
        y += fm2.height() + 1
        p.drawText(QRect(tx, y, tw_, fm2.height() + 2), Qt.AlignLeft | Qt.AlignVCenter,
                   fm2.elidedText(it.name, Qt.ElideMiddle, tw_))
        # border + checkbox
        sel = it.selected
        cur = (self.view.currentIndex() == index) and self.view.hasFocus()
        if sel or cur:
            pen = QPen(QColor(ACCENT if sel else "#8fb4ff"), 3 if sel else 2)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(card).adjusted(1.5, 1.5, -1.5, -1.5), 10, 10)
        cb = self.check_rect(card)
        p.setPen(QPen(QColor("white"), 2))
        p.setBrush(QColor(ACCENT) if sel else QColor(0, 0, 0, 150))
        p.drawRoundedRect(QRectF(cb), 8, 8)
        if sel:
            pen = QPen(QColor("white"), 3.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            p.setPen(pen)
            c = cb
            p.drawPolyline([QPoint(c.left() + 8, c.top() + 18), QPoint(c.left() + 14, c.top() + 25),
                            QPoint(c.left() + 26, c.top() + 10)])
        p.restore()


# ============================================================================ preview popups

class HoverPreview(QLabel):
    """Large floating preview shown on mouse hover / press-and-hold.  Never takes the mouse."""

    def __init__(self, parent):
        super().__init__(parent.window())
        self.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(f"background:#05070b; border:2px solid {ACCENT}; border-radius:10px; padding:6px; color:{TEXT};")
        self._cache: OrderedDict[str, QPixmap] = OrderedDict()

    def load(self, it: core.ImageItem, side: int) -> QPixmap | None:
        key = f"{it.order}:{side}"
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        dpr = self.devicePixelRatioF() or 1.0
        img, _, _ = imgutil.read_image(it.path, int(side * dpr))
        if img is None:
            return None
        img = imgutil.flatten(img)
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(dpr)
        self._cache[key] = pm
        while len(self._cache) > 10:
            self._cache.popitem(last=False)
        return pm

    def show_item(self, it: core.ImageItem, anchor: QRect, area: QRect):
        """anchor/area in global coordinates."""
        side = max(260, min(560, int(min(area.width(), area.height()) * 0.78)))
        pm = self.load(it, side)
        if pm is not None:
            self.setPixmap(pm)
            sz = QSize(round(pm.width() / pm.devicePixelRatio()) + 16, round(pm.height() / pm.devicePixelRatio()) + 16)
            self.setText("")
        else:
            self.setPixmap(QPixmap())
            self.setText("No preview available for this format\n(it will still be saved)")
            sz = QSize(300, 120)
        self.resize(sz)
        x = anchor.right() + 12
        if x + sz.width() > area.right():
            x = anchor.left() - sz.width() - 12
        if x < area.left():
            x = area.left() + (area.width() - sz.width()) // 2
        y = anchor.top() + (anchor.height() - sz.height()) // 2
        y = max(area.top() + 4, min(y, area.bottom() - sz.height() - 4))
        self.move(x, y)
        self.show()
        self.raise_()


class PreviewDialog(QDialog):
    """Big preview with Prev / Next / Select - the touch friendly way to choose correctly."""

    def __init__(self, parent, model: ItemModel, row: int):
        super().__init__(parent)
        self.setWindowTitle("Preview")
        self.setModal(True)
        self.model = model
        self.row = row
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        self.img = QLabel(alignment=Qt.AlignCenter)
        self.img.setMinimumSize(300, 200)
        self.img.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.img.setStyleSheet("background:#05070b; border-radius:10px;")
        lay.addWidget(self.img, 1)
        self.info = QLabel(alignment=Qt.AlignCenter)
        self.info.setWordWrap(True)
        self.info.setStyleSheet(f"color:{MUTED};")
        lay.addWidget(self.info)
        bar = QHBoxLayout()
        self.prev = QPushButton("\u25c0  Previous")
        self.next = QPushButton("Next  \u25b6")
        self.sel = QPushButton("")
        self.sel.setObjectName("primary")
        self.close_b = QPushButton("Close")
        for b in (self.prev, self.next, self.sel, self.close_b):
            b.setMinimumHeight(48)
        bar.addWidget(self.prev)
        bar.addWidget(self.next)
        bar.addStretch(1)
        bar.addWidget(self.sel)
        bar.addWidget(self.close_b)
        lay.addLayout(bar)
        self.prev.clicked.connect(lambda: self.go(-1))
        self.next.clicked.connect(lambda: self.go(1))
        self.sel.clicked.connect(self.toggle)
        self.close_b.clicked.connect(self.accept)
        QShortcut(QKeySequence(Qt.Key_Left), self, activated=lambda: self.go(-1))
        QShortcut(QKeySequence(Qt.Key_Right), self, activated=lambda: self.go(1))
        QShortcut(QKeySequence(Qt.Key_Space), self, activated=self.toggle)
        QShortcut(QKeySequence(Qt.Key_Return), self, activated=self.toggle)
        win = parent.window().size()
        self.resize(int(win.width() * 0.88), int(win.height() * 0.9))
        self._pm_cache: dict[int, QPixmap] = {}
        self.show_row()

    def current(self):
        return self.model.view[self.row] if 0 <= self.row < len(self.model.view) else None

    def go(self, d):
        n = len(self.model.view)
        if n:
            self.row = (self.row + d) % n
            self.show_row()

    def toggle(self):
        self.model.toggle_row(self.row)
        self.refresh_sel()

    def refresh_sel(self):
        it = self.current()
        if it:
            self.sel.setText("\u2714  Selected - tap to unselect" if it.selected else "\u2610  Select this image")

    def show_row(self):
        it = self.current()
        if not it:
            return
        self.info.setText(f"{self.row + 1} of {len(self.model.view)}  \u00b7  {it.dims_text}  \u00b7  {it.type_text}  \u00b7  "
                          f"{core.human_size(it.size)}  \u00b7  {it.name}\n{it.url[:160]}")
        self.refresh_sel()
        self.render_img()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self.render_img)

    def render_img(self):
        it = self.current()
        if not it:
            return
        dpr = self.devicePixelRatioF() or 1.0
        box = self.img.size()
        side = int(max(box.width(), box.height()) * dpr)
        img, _, _ = imgutil.read_image(it.path, max(300, side))
        if img is None:
            self.img.setPixmap(QPixmap())
            self.img.setText("No preview available for this format.\nIt will still be saved as the original file.")
            return
        pm = QPixmap.fromImage(imgutil.flatten(img))
        pm = pm.scaled(int(box.width() * dpr), int(box.height() * dpr), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        pm.setDevicePixelRatio(dpr)
        self.img.setText("")
        self.img.setPixmap(pm)


# ============================================================================ grid view

class GridView(QListView):
    """Thumbnail grid.  Mouse wheel, touch/mouse drag with fling, PgUp/PgDn/Home/End, arrows, Space all scroll/select.
    A click toggles selection; hover (mouse) or press-and-hold (touch) shows a large preview."""

    toggled = Signal(int)
    previewRequested = Signal(int)

    DRAG_PX = 10
    HOLD_MS = 520
    HOVER_MS = 650

    def __init__(self):
        super().__init__()
        self.setViewMode(QListView.IconMode)
        self.setFlow(QListView.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setUniformItemSizes(True)
        self.setSpacing(0)
        self.setSelectionMode(QAbstractItemView.NoSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(40)
        self.card_w = CARD_W["Medium"]
        self._delegate = CardDelegate(self)
        self.setItemDelegate(self._delegate)
        self._gen = 0
        self._pm_prefix = f"wig{id(self)}-0"
        QPixmapCache.setCacheLimit(300 * 1024)
        self._press: QPoint | None = None
        self._press_idx = QModelIndex()
        self._dragging = False
        self._held = False
        self._last_y = 0
        self._samples: list[tuple[float, int]] = []
        self._hold = QTimer(self, singleShot=True, interval=self.HOLD_MS)
        self._hold.timeout.connect(self._on_hold)
        self._hover = QTimer(self, singleShot=True, interval=self.HOVER_MS)
        self._hover.timeout.connect(self._on_hover)
        self._hover_idx = QModelIndex()
        self._fling = QTimer(self, interval=15)
        self._fling.timeout.connect(self._fling_step)
        self._vel = 0.0
        self.popup = HoverPreview(self)
        self.hover_enabled = True

    def reset_cache(self):
        """New scan: forget cached pixmaps (item numbers are reused)."""
        self._gen += 1
        self._pm_prefix = f"wig{id(self)}-{self._gen}"
        self.popup._cache.clear()
        self.popup.hide()

    # ---- sizes / pixmaps
    def cell_size(self) -> QSize:
        """Cells grow a little so each row exactly fills the width (no ragged empty strip on the right)."""
        vw = max(1, self.viewport().width() - 2)
        base = self.card_w + 10
        cols = max(1, vw // base)
        w = max(base, vw // cols)
        return QSize(w, round((w - 10) * 0.75) + 12 + 66 + 10)

    def set_card_width(self, w: int):
        self.card_w = w
        self._relayout()

    def _relayout(self):
        sz = self.cell_size()
        if sz != self.gridSize():
            self.setGridSize(sz)
            self.doItemsLayout()
        self.viewport().update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    def pixmap_for(self, it: core.ImageItem) -> QPixmap | None:
        if not it.thumb:
            return None
        key = f"{self._pm_prefix}:{it.order}"
        pm = QPixmap()
        if QPixmapCache.find(key, pm):
            return pm
        pm = imgutil.pixmap_from_bytes(it.thumb)
        QPixmapCache.insert(key, pm)
        return pm

    # ---- scrolling helpers
    def page(self, direction: int):
        vsb = self.verticalScrollBar()
        step = max(80, int(self.viewport().height() * 0.9))
        vsb.setValue(vsb.value() + direction * step)

    def wheelEvent(self, e):
        self._fling.stop()
        self._hide_popup()
        vsb = self.verticalScrollBar()
        pd = e.pixelDelta().y()
        dy = pd if pd else e.angleDelta().y() * 1.2   # a wheel notch (120) ~ 144 px
        vsb.setValue(int(vsb.value() - dy))
        e.accept()

    def keyPressEvent(self, e):
        k = e.key()
        vsb = self.verticalScrollBar()
        if k == Qt.Key_PageDown:
            self.page(1)
        elif k == Qt.Key_PageUp:
            self.page(-1)
        elif k == Qt.Key_Home:
            vsb.setValue(0)
            if self.model().rowCount():
                self.setCurrentIndex(self.model().index(0))
        elif k == Qt.Key_End:
            vsb.setValue(vsb.maximum())
            n = self.model().rowCount()
            if n:
                self.setCurrentIndex(self.model().index(n - 1))
        elif k in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            if self.currentIndex().isValid():
                self.toggled.emit(self.currentIndex().row())
        elif k == Qt.Key_P:
            if self.currentIndex().isValid():
                self.previewRequested.emit(self.currentIndex().row())
        elif k in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            m = self.model()
            n = m.rowCount()
            if not n:
                return
            cur = self.currentIndex().row() if self.currentIndex().isValid() else -1
            if cur < 0:
                nxt = 0
            else:
                per = max(1, self.viewport().width() // max(1, self.cell_size().width()))
                nxt = cur + {Qt.Key_Left: -1, Qt.Key_Right: 1, Qt.Key_Up: -per, Qt.Key_Down: per}[k]
                nxt = max(0, min(n - 1, nxt))
            self.setCurrentIndex(m.index(nxt))
            self.scrollTo(m.index(nxt), QAbstractItemView.EnsureVisible)
            self.viewport().update()
        else:
            super().keyPressEvent(e)

    # ---- pointer: click / drag-scroll (touch + mouse) / hold / hover
    def _index_at(self, pos: QPoint) -> QModelIndex:
        ix = self.indexAt(pos)
        return ix

    def _hide_popup(self):
        self._hover.stop()
        self._hold.stop()
        if self.popup.isVisible():
            self.popup.hide()

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        self.setFocus(Qt.MouseFocusReason)
        self._fling.stop()
        self._press = e.position().toPoint()
        self._press_idx = self._index_at(self._press)
        self._dragging = False
        self._held = False
        self._last_y = e.position().y()
        self._samples = [(time.monotonic(), e.position().y())]
        self._hover.stop()
        if self.popup.isVisible():
            self.popup.hide()
        if self._press_idx.isValid():
            self.setCurrentIndex(self._press_idx)
            self._hold.start()
        e.accept()

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        if self._press is not None and (e.buttons() & Qt.LeftButton):
            if not self._dragging and not self._held and (pos - self._press).manhattanLength() > self.DRAG_PX:
                self._dragging = True
                self._hold.stop()
                self._hide_popup()
            if self._dragging:
                dy = e.position().y() - self._last_y
                self._last_y = e.position().y()
                vsb = self.verticalScrollBar()
                vsb.setValue(int(vsb.value() - dy))
                now = time.monotonic()
                self._samples.append((now, e.position().y()))
                self._samples = [s for s in self._samples if now - s[0] < 0.12]
            e.accept()
            return
        # plain hover
        if self.hover_enabled:
            ix = self._index_at(pos)
            if ix != self._hover_idx:
                self._hover_idx = ix
                if self.popup.isVisible():
                    self.popup.hide()
                self._hover.stop()
                if ix.isValid():
                    self._hover.start()
        e.accept()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or self._press is None:
            return
        self._hold.stop()
        was_drag, was_held = self._dragging, self._held
        idx = self._press_idx
        self._press = None
        self._dragging = False
        if self.popup.isVisible() and was_held:
            self.popup.hide()
        if was_drag:
            if len(self._samples) >= 2:
                t0, y0 = self._samples[0]
                t1, y1 = self._samples[-1]
                if t1 - t0 > 0.005:
                    self._vel = -(y1 - y0) / (t1 - t0)       # px/s
                    if abs(self._vel) > 250:
                        self._fling.start()
        elif not was_held and idx.isValid() and idx == self._index_at(e.position().toPoint()):
            self.toggled.emit(idx.row())
        e.accept()

    def mouseDoubleClickEvent(self, e):
        e.accept()
        # a double click is two clicks (toggled twice); open the big preview instead of toggling a third time
        ix = self._index_at(e.position().toPoint())
        if ix.isValid():
            self.previewRequested.emit(ix.row())

    def leaveEvent(self, e):
        self._hover_idx = QModelIndex()
        if self._press is None:
            self._hide_popup()
        super().leaveEvent(e)

    def _fling_step(self):
        vsb = self.verticalScrollBar()
        dt = 0.015
        nv = vsb.value() + int(self._vel * dt)
        vsb.setValue(nv)
        self._vel *= 0.94
        if abs(self._vel) < 40 or nv <= 0 or nv >= vsb.maximum():
            self._fling.stop()

    def _show_popup_for(self, ix: QModelIndex):
        it = self.model().data(ix, Qt.UserRole)
        if it is None:
            return
        rect = self.visualRect(ix)
        card = self._delegate.card_rect(rect)
        tl = self.viewport().mapToGlobal(card.topLeft())
        anchor = QRect(tl, card.size())
        w = self.window()
        area = QRect(w.mapToGlobal(QPoint(0, 0)), w.size())
        self.popup.show_item(it, anchor, area)

    def _on_hold(self):
        if self._press is not None and not self._dragging and self._press_idx.isValid():
            self._held = True
            self._show_popup_for(self._press_idx)

    def _on_hover(self):
        if self._press is None and self._hover_idx.isValid() and self.underMouse():
            self._show_popup_for(self._hover_idx)

    def hide_all(self):
        self._hide_popup()


# ============================================================================ workers

class ScanWorker(QObject):
    status = Signal(str)
    progress = Signal(int, int)
    item = Signal(object)
    done = Signal(object, str)       # summary, error text

    def __init__(self, tmpdir, page_url, urls, html_text, html_base, opts):
        super().__init__()
        self.args = (page_url, urls, html_text, html_base, opts)
        self.scanner = core.Scanner(tmpdir, self.status.emit, self.progress.emit, self.item.emit)

    def run(self):
        page_url, urls, html_text, html_base, opts = self.args
        err = ""
        try:
            self.scanner.run(urls, page_url, opts, html_text=html_text, html_base=html_base)
        except net.FetchError as e:
            err = str(e)
        except Exception as e:                   # noqa
            import traceback
            err = f"Unexpected problem: {e}"
            self.scanner.summary.notes.append(traceback.format_exc())
        self.done.emit(self.scanner.summary, err)

    def cancel(self):
        self.scanner.cancel.set()


class SaveWorker(QObject):
    progress = Signal(int, int, str)
    done = Signal(object)

    def __init__(self, items, folder, convert, session, referer):
        super().__init__()
        self.items, self.folder, self.convert, self.session, self.referer = items, folder, convert, session, referer
        self.cancel_ev = threading.Event()

    def run(self):
        try:
            res = core.save_items(self.items, self.folder, self.convert, self.cancel_ev, self.progress.emit,
                                  self.session, self.referer)
        except Exception as e:                   # noqa
            res = core.SaveResult(folder=self.folder)
            res.failed.append(("", f"{e}"))
        self.done.emit(res)

    def cancel(self):
        self.cancel_ev.set()


# ============================================================================ dialogs

class UrlListDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Paste image links")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Paste image URLs, one per line (they will be downloaded and shown as thumbnails):"))
        self.text = QPlainTextEdit()
        self.text.setMinimumSize(640, 280)
        lay.addWidget(self.text, 1)
        bb = QDialogButtonBox()
        self.ok = bb.addButton("Load these images", QDialogButtonBox.AcceptRole)
        self.ok.setObjectName("primary")
        bb.addButton("Cancel", QDialogButtonBox.RejectRole)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        cb = QGuiApplication.clipboard().text()
        if extract.parse_url_list(cb):
            self.text.setPlainText(cb)


class OptionsDialog(QDialog):
    def __init__(self, parent, prefer_original, include_links):
        super().__init__(parent)
        self.setWindowTitle("Options")
        lay = QVBoxLayout(self)
        self.c1 = QCheckBox("Prefer the original full-size file for resized Wikimedia/Wikipedia thumbnails")
        self.c1.setChecked(prefer_original)
        self.c2 = QCheckBox("Include images that the page links to (<a href=\"photo.jpg\">)")
        self.c2.setChecked(include_links)
        lay.addWidget(self.c1)
        lay.addWidget(self.c2)
        n = QLabel("Plain mode sends a normal browser User-Agent and the page as Referer, follows redirects and keeps cookies.\n"
                   "Tip: unticked 'Render page' is faster; tick it for sites that build their pictures with JavaScript.")
        n.setStyleSheet(f"color:{MUTED};")
        n.setWordWrap(True)
        lay.addWidget(n)
        bb = QDialogButtonBox()
        ok = bb.addButton("OK", QDialogButtonBox.AcceptRole)
        ok.setObjectName("primary")
        bb.addButton("Cancel", QDialogButtonBox.RejectRole)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


# ============================================================================ main window

class MainWindow(QWidget):
    def __init__(self, auto: dict | None = None):
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setAcceptDrops(True)
        self.auto = auto or {}
        self.tmpdir = net.make_tmpdir()
        if self.auto:      # automated runs never touch the user's real settings
            self.settings = QSettings(str(self.tmpdir / "auto-settings.ini"), QSettings.IniFormat)
        else:
            self.settings = QSettings("WebImageGrabber", "WebImageGrabber")
        self.thread: QThread | None = None
        self.worker = None
        self.save_thread: QThread | None = None
        self.save_worker = None
        self.summary: core.ScanSummary | None = None
        self.session_ref = None
        self.referer = ""
        self.page_title = ""
        self.page_url = ""
        self.busy = False
        self.last_saved_folder: Path | None = None
        self.prefer_original = self.settings.value("prefer_original", True, bool)
        self.include_links = self.settings.value("include_links", True, bool)
        self.auto_report: dict = {}

        self.model = ItemModel()
        self._build()
        self._load_settings()
        self.model.changed.connect(self._update_counts)
        self.flush = QTimer(self, interval=250)
        self.flush.timeout.connect(self._flush)
        self.flush.start()
        QShortcut(QKeySequence(Qt.Key_PageDown), self, activated=lambda: self.grid.page(1))
        QShortcut(QKeySequence(Qt.Key_PageUp), self, activated=lambda: self.grid.page(-1))
        QShortcut(QKeySequence("Ctrl+A"), self, activated=lambda: self.model.set_selected("all"))
        QShortcut(QKeySequence("Ctrl+L"), self, activated=lambda: (self.url.setFocus(), self.url.selectAll()))
        QShortcut(QKeySequence("F5"), self, activated=self.start_fetch)
        self._update_counts()

    # ---- UI
    def _build(self):
        self.setStyleSheet(STYLE)
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        # row 1: address
        r1 = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("Paste a web page address (https://...) and press Fetch")
        self.url.setClearButtonEnabled(True)
        self.url.returnPressed.connect(self.start_fetch)
        self.paste_b = QPushButton("Paste")
        self.paste_b.clicked.connect(self.paste_url)
        self.fetch_b = QPushButton("Fetch images")
        self.fetch_b.setObjectName("primary")
        self.fetch_b.setMinimumWidth(150)
        self.fetch_b.clicked.connect(self.start_fetch)
        self.stop_b = QPushButton("Stop")
        self.stop_b.setObjectName("danger")
        self.stop_b.clicked.connect(self.stop_scan)
        self.stop_b.setVisible(False)
        self.render_cb = QCheckBox("Render page (JavaScript)")
        self.render_cb.setToolTip("Opens the page in a hidden Edge/Chrome, scrolls to load lazy images, then reads the result.\nSlower - use it when plain mode finds too few images.")
        self.list_b = QPushButton("Paste URL list\u2026")
        self.list_b.clicked.connect(self.paste_list)
        self.opt_b = QPushButton("Options\u2026")
        self.opt_b.clicked.connect(self.open_options)
        r1.addWidget(self.url, 1)
        r1.addWidget(self.paste_b)
        r1.addWidget(self.render_cb)
        r1.addWidget(self.fetch_b)
        r1.addWidget(self.stop_b)
        root.addLayout(r1)

        # row 2: mode + filters
        r2 = QHBoxLayout()
        r2.setSpacing(8)
        self.minw = QSpinBox()
        self.minw.setRange(0, 20000)
        self.minw.setSingleStep(100)
        self.minw.setPrefix("Min width ")
        self.minh = QSpinBox()
        self.minh.setRange(0, 20000)
        self.minh.setSingleStep(100)
        self.minh.setPrefix("Min height ")
        for s in (self.minw, self.minh):
            s.setMinimumWidth(170)
            s.valueChanged.connect(self._filters_changed)
            r2.addWidget(s)
        self.type_btns: dict[str, QToolButton] = {}
        for name in ["JPG", "PNG", "WEBP", "GIF", "SVG", "OTHER"]:
            b = QToolButton()
            b.setText(name.title() if name == "OTHER" else name)
            b.setCheckable(True)
            b.setChecked(True)
            b.setMinimumWidth(66)
            b.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
            b.toggled.connect(self._filters_changed)
            self.type_btns[name] = b
            r2.addWidget(b)
        self.tiny_cb = QCheckBox("Include tiny (<16 px)")
        self.tiny_cb.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.tiny_cb.toggled.connect(self._filters_changed)
        r2.addWidget(self.tiny_cb)
        r2.addStretch(1)
        self.sort_cb = QComboBox()
        self.sort_cb.addItems(SORTS)
        self.sort_cb.currentIndexChanged.connect(self._filters_changed)
        self.sort_cb.setMinimumWidth(200)
        self.size_cb = QComboBox()
        self.size_cb.addItems(list(CARD_W.keys()))
        self.size_cb.setCurrentText("Medium")
        self.size_cb.currentTextChanged.connect(lambda t: self.grid.set_card_width(CARD_W[t]))
        r2.addWidget(QLabel("Sort"))
        r2.addWidget(self.sort_cb)
        root.addLayout(r2)

        # row 3: selection actions
        r3 = QHBoxLayout()
        r3.setSpacing(8)
        self.all_b = QPushButton("Select all")
        self.none_b = QPushButton("Select none")
        self.inv_b = QPushButton("Invert")
        self.prev_b = QPushButton("\U0001F50D  Preview")
        self.prev_b.setToolTip("Big preview of the highlighted image (or tap-and-hold / hover a thumbnail)")
        self.all_b.clicked.connect(lambda: self.model.set_selected("all"))
        self.none_b.clicked.connect(lambda: self.model.set_selected("none"))
        self.inv_b.clicked.connect(lambda: self.model.set_selected("invert"))
        self.prev_b.clicked.connect(self.open_preview)
        for b in (self.all_b, self.none_b, self.inv_b, self.prev_b):
            r3.addWidget(b)
        self.count_l = QLabel("")
        self.count_l.setStyleSheet("font-weight:600;")
        r3.addSpacing(10)
        r3.addWidget(self.count_l, 1)
        r3.addWidget(QLabel("Tiles"))
        r3.addWidget(self.size_cb)
        r3.addWidget(self.list_b)
        r3.addWidget(self.opt_b)
        root.addLayout(r3)

        # grid
        self.grid = GridView()
        self.grid.setModel(self.model)
        self.grid.set_card_width(CARD_W["Medium"])
        self.grid.toggled.connect(self.model.toggle_row)
        self.grid.previewRequested.connect(self.open_preview_row)
        self.empty_l = QLabel("Paste a page address above and press Fetch images.\nThumbnails appear here - tap to select, hold (or hover) for a big preview.",
                              self.grid.viewport())
        self.empty_l.setAlignment(Qt.AlignCenter)
        self.empty_l.setStyleSheet(f"color:{MUTED}; font-size:13pt;")
        self.empty_l.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        root.addWidget(self.grid, 1)

        # status strip
        r4 = QHBoxLayout()
        self.status_l = QLabel("Ready.")
        self.status_l.setStyleSheet(f"color:{MUTED};")
        self.status_l.setMinimumWidth(200)
        self.status_l.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.details_b = QPushButton("Details")
        self.details_b.setVisible(False)
        self.details_b.clicked.connect(self.show_details)
        self.details_b.setMinimumHeight(30)
        self.bar = QProgressBar()
        self.bar.setMinimumWidth(320)
        self.bar.setMaximumWidth(420)
        self.bar.setValue(0)
        r4.addWidget(self.status_l, 1)
        r4.addWidget(self.details_b)
        r4.addWidget(self.bar)
        root.addLayout(r4)

        # save row
        r5 = QHBoxLayout()
        r5.setSpacing(8)
        r5.addWidget(QLabel("Save to"))
        self.folder = QLineEdit()
        self.folder.setPlaceholderText("Choose a folder\u2026")
        self.browse_b = QPushButton("Browse\u2026")
        self.browse_b.clicked.connect(self.browse)
        self.sub_cb = QCheckBox("Subfolder named after the page")
        self.conv_cb = QComboBox()
        self.conv_cb.addItems(["WebP: keep as is", "WebP \u2192 JPG", "WebP \u2192 PNG"])
        self.conv_cb.setToolTip("Convert still WebP images when saving (animated WebP is always kept as is)")
        self.save_b = QPushButton("Save selected")
        self.save_b.setObjectName("primary")
        self.save_b.setMinimumWidth(190)
        self.save_b.clicked.connect(self.start_save)
        self.cancel_b = QPushButton("Cancel")
        self.cancel_b.setObjectName("danger")
        self.cancel_b.clicked.connect(self.cancel_save)
        self.cancel_b.setEnabled(False)
        self.open_b = QPushButton("Open folder")
        self.open_b.clicked.connect(self.open_folder)
        r5.addWidget(self.folder, 1)
        r5.addWidget(self.browse_b)
        r5.addWidget(self.sub_cb)
        r5.addWidget(self.conv_cb)
        r5.addWidget(self.save_b)
        r5.addWidget(self.cancel_b)
        r5.addWidget(self.open_b)
        root.addLayout(r5)
        self.setMinimumSize(980, 600)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.empty_l.setGeometry(self.grid.viewport().rect())

    # ---- settings
    def _load_settings(self):
        s = self.settings
        default = Path(QGuiApplication.instance().property("pictures_dir") or Path.home() / "Pictures") / "WebImageGrabber"
        self.folder.setText(s.value("folder", str(default)))
        self.render_cb.setChecked(s.value("render", False, bool))
        self.sub_cb.setChecked(s.value("subfolder", False, bool))
        self.conv_cb.setCurrentIndex(int(s.value("convert", 0)))
        self.size_cb.setCurrentText(s.value("tile", "Medium"))
        self.sort_cb.setCurrentIndex(int(s.value("sort", 0)))
        self.tiny_cb.setChecked(s.value("tiny", False, bool))
        self.grid.set_card_width(CARD_W.get(self.size_cb.currentText(), 200))
        self.model.sort = self.sort_cb.currentIndex()

    def _save_settings(self):
        s = self.settings
        s.setValue("folder", self.folder.text())
        s.setValue("render", self.render_cb.isChecked())
        s.setValue("subfolder", self.sub_cb.isChecked())
        s.setValue("convert", self.conv_cb.currentIndex())
        s.setValue("tile", self.size_cb.currentText())
        s.setValue("sort", self.sort_cb.currentIndex())
        s.setValue("tiny", self.tiny_cb.isChecked())
        s.setValue("prefer_original", self.prefer_original)
        s.setValue("include_links", self.include_links)

    def closeEvent(self, e):
        self.stop_scan()
        self.cancel_save()
        self._save_settings()
        self.grid.hide_all()
        for t in (self.thread, self.save_thread):
            if t is not None:
                t.quit()
                t.wait(3000)
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        e.accept()

    # ---- helpers
    def set_status(self, text: str):
        self.status_l.setText(text)
        self.status_l.setToolTip(text)

    def paste_url(self):
        t = QGuiApplication.clipboard().text().strip()
        if t:
            self.url.setText(t.splitlines()[0] if len(t.splitlines()) == 1 else t)
            if len(extract.parse_url_list(t)) > 1:
                self.fetch_list(extract.parse_url_list(t))

    def paste_list(self):
        d = UrlListDialog(self)
        if d.exec() == QDialog.Accepted:
            urls = extract.parse_url_list(d.text.toPlainText())
            if not urls:
                QMessageBox.information(self, APP_NAME, "No image links (http/https) found in the text.")
                return
            self.fetch_list(urls)

    def open_options(self):
        d = OptionsDialog(self, self.prefer_original, self.include_links)
        if d.exec() == QDialog.Accepted:
            self.prefer_original, self.include_links = d.c1.isChecked(), d.c2.isChecked()
            self._save_settings()

    def _filters_changed(self, *_):
        m = self.model
        m.min_w, m.min_h = self.minw.value(), self.minh.value()
        m.types = {n for n, b in self.type_btns.items() if b.isChecked()}
        m.include_tiny = self.tiny_cb.isChecked()
        m.sort = self.sort_cb.currentIndex()
        m.refresh(self.grid.verticalScrollBar())

    def _flush(self):
        if self.model.dirty:
            self.model.refresh(self.grid.verticalScrollBar())

    def _update_counts(self):
        n = len(self.model.view)
        total = len(self.model.all)
        sel = len(self.model.selected_visible())
        hidden = total - n
        txt = f"Selected {sel} of {n} shown"
        if hidden > 0:
            txt += f"  ({hidden} hidden by filters)"
        self.count_l.setText(txt)
        self.save_b.setText(f"Save selected ({sel})")
        self.save_b.setEnabled(sel > 0 and not self.busy and self.save_thread is None)
        self.empty_l.setVisible(n == 0 and not self.busy)
        if total == 0:
            self.empty_l.setText("Paste a page address above and press Fetch images.\nThumbnails appear here - tap to select, hold (or hover) for a big preview.")
        elif n == 0:
            self.empty_l.setText("All images are hidden by the filters.\nLower Min W/H, tick more types, or tick 'Include tiny'.")
        for b in (self.all_b, self.none_b, self.inv_b, self.prev_b):
            b.setEnabled(n > 0)

    # ---- fetch
    def _begin_scan(self, page_url=None, urls=None, html_text=None, html_base=None):
        if self.busy:
            return
        self.grid.hide_all()
        self.grid.reset_cache()
        self.model.clear()
        self._clear_tmp()
        self.summary = None
        self.details_b.setVisible(False)
        self.busy = True
        self.fetch_b.setEnabled(False)
        self.stop_b.setVisible(True)
        self.bar.setRange(0, 0)
        self.set_status("Starting\u2026")
        self.page_title = ""
        opts = core.ScanOptions(render=self.render_cb.isChecked(), prefer_original=self.prefer_original,
                                include_links=self.include_links)
        self.worker = ScanWorker(self.tmpdir, page_url, urls, html_text, html_base, opts)
        self.session_ref = self.worker.scanner.session
        self.thread = QThread()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.status.connect(self.set_status)
        self.worker.progress.connect(self._on_progress)
        self.worker.item.connect(self.model.add)
        self.worker.done.connect(self._scan_done)
        self.thread.start()
        self._update_counts()

    def _clear_tmp(self):
        for p in self.tmpdir.glob("*"):
            try:
                p.unlink()
            except OSError:
                pass

    def start_fetch(self):
        txt = self.url.text().strip()
        if "\n" in txt or len(extract.parse_url_list(txt)) > 1:
            self.fetch_list(extract.parse_url_list(txt))
            return
        try:
            u = net.normalize_url(txt)
        except net.FetchError as e:
            self.set_status(str(e))
            QMessageBox.information(self, APP_NAME, str(e))
            return
        self.url.setText(u)
        self._begin_scan(page_url=u)

    def fetch_list(self, urls):
        self._begin_scan(urls=urls)

    def fetch_html_file(self, path: str):
        try:
            data = Path(path).read_bytes()
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Could not read that file: {e}")
            return
        txt = data.decode("utf-8", errors="replace")
        self.url.setText(Path(path).as_uri())
        self._begin_scan(html_text=txt, html_base=Path(path).as_uri())

    def stop_scan(self):
        if self.worker is not None and self.busy:
            self.worker.cancel()
            self.set_status("Stopping\u2026")

    def _on_progress(self, done, total):
        if total <= 0:
            self.bar.setRange(0, 0)
            return
        self.bar.setRange(0, total)
        self.bar.setValue(done)
        self.bar.setFormat(f"{done} / {total}")

    def _scan_done(self, summary: core.ScanSummary, err: str):
        self.busy = False
        self.summary = summary
        self.page_title = summary.title
        self.page_url = summary.page_url
        self.fetch_b.setEnabled(True)
        self.stop_b.setVisible(False)
        if self.thread:
            self.thread.quit()
            self.thread.wait(3000)
            self.thread = None
        self.model.refresh(self.grid.verticalScrollBar())
        self.bar.setRange(0, 100)
        self.bar.setValue(0 if err else 100)
        self.bar.setFormat("")
        if err:
            self.set_status(err)
            QMessageBox.warning(self, APP_NAME, err)
        else:
            parts = [f"{summary.loaded} image(s) loaded"]
            if summary.duplicates:
                parts.append(f"{summary.duplicates} duplicate(s) merged")
            if summary.skipped:
                parts.append(f"{summary.skipped} link(s) were not pictures")
            if summary.failed:
                parts.append(f"{len(summary.failed)} could not be downloaded")
            if summary.truncated:
                parts.append(f"only the first {core.MAX_CANDIDATES} links were used")
            hid = len(self.model.all) - len(self.model.view)
            if hid:
                parts.append(f"{hid} hidden by filters")
            if summary.cancelled:
                parts.insert(0, "Stopped")
            self.set_status(" \u00b7 ".join(parts) + ("." if parts else ""))
            if summary.loaded == 0 and summary.failed and not summary.cancelled:
                top = summary.failed[0][1]
                QMessageBox.warning(self, APP_NAME, f"Found {summary.candidates} image link(s) but none could be downloaded ({top}).\n"
                                    "The site may block automated downloads (hotlink protection) - try 'Render page (JavaScript)'.")
        self.details_b.setVisible(bool(summary.failed or summary.notes))
        self._update_counts()
        self._maybe_auto_next()

    def show_details(self):
        s = self.summary
        if not s:
            return
        lines = [f"Page: {s.page_url}", f"Title: {s.title}", f"Image links found: {s.candidates}  loaded: {s.loaded}  duplicates: {s.duplicates}", ""]
        if s.failed:
            lines.append("Could not be downloaded:")
            lines += [f"  {r}  -  {u[:150]}" for u, r in s.failed[:300]]
        lines += s.notes
        d = QDialog(self)
        d.setWindowTitle("Details")
        lay = QVBoxLayout(d)
        t = QPlainTextEdit("\n".join(lines))
        t.setReadOnly(True)
        t.setMinimumSize(760, 380)
        lay.addWidget(t)
        b = QPushButton("Close")
        b.clicked.connect(d.accept)
        lay.addWidget(b)
        d.exec()

    # ---- preview
    def open_preview(self):
        ix = self.grid.currentIndex()
        row = ix.row() if ix.isValid() else 0
        self.open_preview_row(row)

    def open_preview_row(self, row: int):
        if not self.model.view:
            return
        self.grid.hide_all()
        dlg = PreviewDialog(self, self.model, row)
        self.preview_dlg = dlg
        if self.auto.get("keep_dialog"):
            dlg.show()
            return
        dlg.exec()
        self.grid.viewport().update()

    # ---- save
    def browse(self):
        start = self.folder.text().strip() or str(Path.home())
        while start and not Path(start).exists():
            parent = str(Path(start).parent)
            if parent == start:
                break
            start = parent
        d = QFileDialog.getExistingDirectory(self, "Choose the folder to save images in", start)
        if d:
            self.folder.setText(d)
            self._save_settings()
            return True
        return False

    def start_save(self):
        items = self.model.selected_visible()
        if not items:
            return
        folder_txt = self.folder.text().strip()
        if not folder_txt:
            if not self.browse():
                return
            folder_txt = self.folder.text().strip()
        folder = Path(folder_txt)
        if self.sub_cb.isChecked():
            folder = folder / core.page_folder_name(self.page_title, self.page_url)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            test = folder / ".wig-write-test"
            test.write_text("x")
            test.unlink()
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Cannot write to that folder:\n{folder}\n\n{e}\nChoose another folder with Browse.")
            return
        conv = ["keep", "jpg", "png"][self.conv_cb.currentIndex()]
        self._save_settings()
        self.save_worker = SaveWorker(items, folder, conv, self.session_ref, self.referer or self.page_url)
        self.save_thread = QThread()
        self.save_worker.moveToThread(self.save_thread)
        self.save_thread.started.connect(self.save_worker.run)
        self.save_worker.progress.connect(self._save_progress)
        self.save_worker.done.connect(self._save_done)
        self.bar.setRange(0, len(items))
        self.bar.setValue(0)
        self.cancel_b.setEnabled(True)
        self.save_b.setEnabled(False)
        self.set_status(f"Saving {len(items)} image(s) to {folder} \u2026")
        self.save_thread.start()

    def _save_progress(self, i, n, name):
        self.bar.setRange(0, max(1, n))
        self.bar.setValue(i)
        self.bar.setFormat(f"{i} / {n}")
        if name:
            self.set_status(f"Saving {name} \u2026")

    def cancel_save(self):
        if self.save_worker is not None:
            self.save_worker.cancel()

    def _save_done(self, res: core.SaveResult):
        if self.save_thread:
            self.save_thread.quit()
            self.save_thread.wait(3000)
        self.save_thread = None
        self.save_worker = None
        self.cancel_b.setEnabled(False)
        self.last_saved_folder = res.folder
        self.bar.setFormat("")
        msg = f"Saved {len(res.saved)} file(s) to {res.folder}"
        if res.cancelled:
            msg = "Cancelled - " + msg
        if res.failed:
            msg += f" ({len(res.failed)} failed: {res.failed[0][1]})"
        self.set_status(msg)
        self.bar.setValue(self.bar.maximum())
        self._update_counts()
        self.saved_result = res
        if not self.auto.get("headless_dialogs"):
            if not res.failed and not res.cancelled and not self.auto:
                pass
        self._maybe_auto_next()

    def open_folder(self):
        f = self.last_saved_folder or (Path(self.folder.text()) if self.folder.text() else None)
        if f is None:
            return
        f.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(f)))

    # ---- drag & drop
    def dragEnterEvent(self, e):
        md = e.mimeData()
        if md.hasUrls() or md.hasText():
            e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        if md.hasUrls():
            for u in md.urls():
                if u.isLocalFile() and u.toLocalFile().lower().endswith((".html", ".htm", ".xhtml", ".mhtml")):
                    self.fetch_html_file(u.toLocalFile())
                    return
            for u in md.urls():
                if u.scheme() in ("http", "https"):
                    self.url.setText(u.toString())
                    self.start_fetch()
                    return
        if md.hasText():
            urls = extract.parse_url_list(md.text())
            if len(urls) > 1:
                self.fetch_list(urls)
            elif urls:
                self.url.setText(urls[0])
                self.start_fetch()

    # ---- automation (selftest / screenshots)
    def run_auto(self):
        """Hidden test hook: drive the real UI the way a user would."""
        a = self.auto
        self.auto_state = "fetch"
        self.auto_report = {"steps": []}
        if a.get("size"):
            w, h = a["size"]
            self.resize(w, h)
        self.show()
        QApplication.processEvents()
        if a.get("folder"):
            self.folder.setText(a["folder"])
        self.render_cb.setChecked(bool(a.get("render")))
        self.sub_cb.setChecked(bool(a.get("subfolder")))
        if a.get("convert"):
            self.conv_cb.setCurrentIndex({"keep": 0, "jpg": 1, "png": 2}[a["convert"]])
        if a.get("tiny"):
            self.tiny_cb.setChecked(True)
        if a.get("min_w"):
            self.minw.setValue(a["min_w"])
        if a.get("html_file"):
            self.fetch_html_file(a["html_file"])
        elif a.get("urls_file"):
            self.fetch_list(extract.parse_url_list(Path(a["urls_file"]).read_text()))
        else:
            self.url.setText(a["url"])
            self.start_fetch()
        QTimer(self, singleShot=True, interval=int(a.get("timeout", 240)) * 1000, timeout=self._auto_timeout).start()
        self._auto_timer = QTimer(self, singleShot=True)

    def _shot(self, name: str):
        d = self.auto.get("shots_dir")
        if not d:
            return
        Path(d).mkdir(parents=True, exist_ok=True)
        QApplication.processEvents()
        p = Path(d) / name
        self.grab().save(str(p))
        self.auto_report.setdefault("screenshots", []).append(str(p))

    def _auto_timeout(self):
        self.auto_report["error"] = "timeout"
        self._auto_finish(False)

    def _maybe_auto_next(self):
        a = self.auto
        if not a:
            return
        if self.auto_state == "fetch":
            if self.summary is not None and self.summary.cancelled is False and not self.busy:
                self.auto_state = "select"
                QTimer.singleShot(600, self._auto_select)
        elif self.auto_state == "saving":
            self._auto_after_save()

    def _auto_select(self):
        from PySide6.QtTest import QTest
        a = self.auto
        m = self.model
        r = self.auto_report
        r["found"] = [{"order": it.order, "name": it.name, "url": it.url, "w": it.w, "h": it.h, "type": it.kind,
                       "bytes": it.size, "source": it.source} for it in m.all]
        r["visible"] = [{"order": it.order, "name": it.name, "w": it.w, "h": it.h, "type": it.kind, "bytes": it.size} for it in m.view]
        r["summary"] = {"candidates": self.summary.candidates, "loaded": self.summary.loaded,
                        "failed": self.summary.failed, "duplicates": self.summary.duplicates, "title": self.summary.title,
                        "status_text": self.status_l.text()}
        self.grid.verticalScrollBar().setValue(0)
        QApplication.processEvents()
        self._shot(a.get("shot_prefix", "") + "01_thumbnails.png")
        if a.get("scroll_test"):
            self._auto_scroll_test()
        # hover popup shot
        if a.get("shots_dir") and m.view:
            ix = m.index(0)
            self.grid._show_popup_for(ix)
            QApplication.processEvents()
            d = Path(a["shots_dir"])
            p = d / (a.get("shot_prefix", "") + "02_hover_preview.png")
            self.grab().save(str(p))
            # composite: popup is a separate top-level window
            try:
                scr = self.screen().grabWindow(0)
                if not scr.isNull():
                    scr.save(str(p))
            except Exception:
                pass
            self.grid.hide_all()
        if a.get("shots_dir") and m.view:
            dlg = PreviewDialog(self, m, min(1, len(m.view) - 1))
            dlg.show()
            QApplication.processEvents()
            time.sleep(0.2)
            QApplication.processEvents()
            dlg.grab().save(str(Path(a["shots_dir"]) / (a.get("shot_prefix", "") + "02c_preview_dialog.png")))
            dlg.close()
        sel = a.get("select")
        idxs = []
        if sel == "all":
            idxs = list(range(len(m.view)))
        elif isinstance(sel, list):
            idxs = [i for i in sel if i < len(m.view)]
        # real mouse clicks on the tiles (scroll into view first)
        picked = []
        for row in idxs:
            ix = m.index(row)
            self.grid.scrollTo(ix, QAbstractItemView.EnsureVisible)
            QApplication.processEvents()
            rect = self.grid.visualRect(ix)
            QTest.mouseClick(self.grid.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center() + QPoint(0, 10))
            QApplication.processEvents()
            picked.append(m.view[row])
        r["selected"] = [{"order": it.order, "name": it.name, "w": it.w, "h": it.h, "bytes": it.size, "sha1": it.sha1} for it in m.selected_visible()]
        self.grid.verticalScrollBar().setValue(0)
        QApplication.processEvents()
        self._shot(a.get("shot_prefix", "") + "03_selected.png")
        if not a.get("save"):
            self._auto_finish(True)
            return
        self.auto_state = "saving"
        self.save_b.click()

    def _auto_scroll_test(self):
        """Wheel, PgUp/PgDn, touch-style drag (+ no accidental select), press-and-hold preview, click-to-select."""
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtTest import QTest
        g = self.grid
        vsb = g.verticalScrollBar()
        vp = g.viewport()
        r = {}
        self.size_cb.setCurrentText("Large")
        QApplication.processEvents()
        vsb.setValue(0)
        r["range_max"] = vsb.maximum()
        r["page_step_visible_height"] = vp.height()
        sel0 = len(self.model.selected_visible())

        def settle(ms=60):
            end = time.time() + ms / 1000
            while time.time() < end:
                QApplication.processEvents()

        c = vp.rect().center()
        ev = QWheelEvent(QPointF(c), QPointF(vp.mapToGlobal(c)), QPoint(0, 0), QPoint(0, -120), Qt.NoButton, Qt.NoModifier,
                         Qt.NoScrollPhase, False)
        QApplication.sendEvent(vp, ev)
        settle()
        r["wheel_down"] = vsb.value()
        ev = QWheelEvent(QPointF(c), QPointF(vp.mapToGlobal(c)), QPoint(0, 0), QPoint(0, 120), Qt.NoButton, Qt.NoModifier,
                         Qt.NoScrollPhase, False)
        QApplication.sendEvent(vp, ev)
        settle()
        r["wheel_up"] = vsb.value()
        g.setFocus()
        QTest.keyClick(g, Qt.Key_PageDown)
        settle()
        r["pgdn"] = vsb.value()
        QTest.keyClick(g, Qt.Key_PageUp)
        settle()
        r["pgup"] = vsb.value()
        QTest.keyClick(g, Qt.Key_End)
        settle()
        r["end"] = vsb.value()
        QTest.keyClick(g, Qt.Key_Home)
        settle()
        r["home"] = vsb.value()
        # touch-style drag: press, move up 150 px in steps, release (must scroll, must NOT select)
        start = QPoint(c.x(), c.y() + 80)
        QTest.mousePress(vp, Qt.LeftButton, Qt.NoModifier, start)
        for dy in range(10, 160, 15):
            QTest.mouseMove(vp, start - QPoint(0, dy))
            QApplication.processEvents()
            time.sleep(0.008)
        QTest.mouseRelease(vp, Qt.LeftButton, Qt.NoModifier, start - QPoint(0, 145))
        g._fling.stop()
        settle()
        r["drag_up"] = vsb.value()
        r["drag_selected_unchanged"] = len(self.model.selected_visible()) == sel0
        vsb.setValue(0)
        settle()
        # press-and-hold shows the big preview (and does not select)
        ix = self.model.index(0)
        rect = g.visualRect(ix)
        p0 = rect.center()
        QTest.mousePress(vp, Qt.LeftButton, Qt.NoModifier, p0)
        end = time.time() + 1.0
        while time.time() < end:
            QApplication.processEvents()
        r["hold_popup_visible"] = g.popup.isVisible()
        if self.auto.get("shots_dir") and g.popup.isVisible():
            try:
                scr = self.screen().grabWindow(0)
                Path(self.auto["shots_dir"]).mkdir(parents=True, exist_ok=True)
                scr.save(str(Path(self.auto["shots_dir"]) / (self.auto.get("shot_prefix", "") + "02b_press_and_hold_preview.png")))
            except Exception:
                pass
        QTest.mouseRelease(vp, Qt.LeftButton, Qt.NoModifier, p0)
        settle()
        r["hold_popup_hidden_after_release"] = not g.popup.isVisible()
        r["hold_selected_unchanged"] = len(self.model.selected_visible()) == sel0
        # plain click toggles
        QTest.mouseClick(vp, Qt.LeftButton, Qt.NoModifier, p0)
        settle()
        r["click_selected"] = len(self.model.selected_visible()) == sel0 + 1
        QTest.mouseClick(vp, Qt.LeftButton, Qt.NoModifier, p0)
        settle()
        r["click_unselected"] = len(self.model.selected_visible()) == sel0
        r["ok"] = (r["range_max"] > 0 and r["wheel_down"] > 0 and r["wheel_up"] < r["wheel_down"] and r["pgdn"] > 0 and
                   r["pgup"] < r["pgdn"] and r["end"] == vsb.maximum() and r["home"] == 0 and r["drag_up"] > 50 and
                   r["drag_selected_unchanged"] and r["hold_popup_visible"] and r["hold_popup_hidden_after_release"] and
                   r["hold_selected_unchanged"] and r["click_selected"] and r["click_unselected"])
        self.auto_report["scroll_test"] = r
        self.size_cb.setCurrentText("Medium")
        vsb.setValue(0)
        QApplication.processEvents()

    def _auto_after_save(self):
        res = getattr(self, "saved_result", None)
        r = self.auto_report
        r["saved"] = [{"name": p.name, "bytes": p.stat().st_size, "sha1": __import__("hashlib").sha1(p.read_bytes()).hexdigest()} for p in (res.saved if res else [])]
        r["save_failed"] = res.failed if res else []
        r["save_folder"] = str(res.folder) if res else ""
        self._shot(self.auto.get("shot_prefix", "") + "04_saved.png")
        self._auto_finish(not (res and res.failed) and self.auto_report.get("scroll_test", {"ok": True}).get("ok", True))

    def _auto_finish(self, ok: bool):
        r = self.auto_report
        r["ok"] = bool(ok)
        rp = self.auto.get("report")
        if rp:
            Path(rp).write_text(json.dumps(r, indent=2, default=str))
        QApplication.processEvents()
        self.auto_exit_code = 0 if ok else 1
        QApplication.instance().exit(self.auto_exit_code)
