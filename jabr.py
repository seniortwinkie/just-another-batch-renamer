#!/usr/bin/env python3
"""
Batch renamer starter (Rename-It! style) built with PySide6.

Features
  * File list with live "original -> new name" preview
  * Drag & drop files/folders into the list (or use Add files)
  * Drag rows to reorder (or Up/Down buttons, Alt+Up / Alt+Down)
  * Stackable filters applied top to bottom, with live preview:
      - Text replace (plain find & replace, no regex)
      - Regex replace
      - Numbering (start, step, padding, template, optional per-folder restart)
  * Natural sort button (numbering follows list order)
  * Safe rename: collision/illegal-name checks, chained renames (a->b, b->c)
    handled via temp names, automatic rollback on failure
  * Presets: save/load the filter stack as JSON (shareable, or pick from the menu)
  * Undo: every rename batch is logged to disk, so you can undo it (Ctrl+Z),
    even after restarting the app

Install:  pip install PySide6
Run:      python jabr.py [files...]
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel, QItemSelectionModel, QMimeData, QModelIndex, QStandardPaths,
    Qt, QUrl, Signal,
)
from PySide6.QtGui import (
    QColor, QDesktopServices, QDrag, QIcon, QKeySequence, QPainter, QPen, QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QPushButton, QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QStyledItemDelegate,
    QTableView, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

ILLEGAL_CHARS = set('<>:"/\\|?*') if os.name == "nt" else {"/"}
TOKEN_RE = re.compile(r"\{(name|n)\}")
MIME_ROWS = "application/x-renamer-rows"  # internal row-reorder drags


# --------------------------------------------------------------------------
# Core logic (no UI imports needed to use this part - easy to unit test / CLI)
# --------------------------------------------------------------------------
@dataclass
class Filter:
    kind: str = "regex"          # "regex" | "text" | "numbering"
    enabled: bool = True
    # text (plain find & replace; also uses ignore_case below)
    find: str = ""
    replace_with: str = ""
    # regex
    pattern: str = ""
    replacement: str = ""
    ignore_case: bool = False
    # numbering
    template: str = "{name}_{n}"  # {n} = number, {name} = current name
    start: int = 1
    step: int = 1
    digits: int = 0               # 0 = auto (pad to fit the largest number)
    per_folder: bool = True       # restart numbering in each folder


@dataclass
class FileContext:
    """Where a file sits in the list: overall, and within its own folder."""
    index: int
    count: int
    folder_index: int
    folder_count: int


class RegexStep:
    def __init__(self, rx: re.Pattern, replacement: str):
        self.rx, self.replacement = rx, replacement

    def __call__(self, name: str, ctx: FileContext) -> str:
        return self.rx.sub(self.replacement, name)


class TextStep:
    """Plain find & replace. Every occurrence is replaced and nothing is special:
    | . ( ) and backslashes in either box are ordinary characters."""

    def __init__(self, find: str, replacement: str, ignore_case: bool):
        self.rx = re.compile(re.escape(find), re.IGNORECASE if ignore_case else 0)
        self.replacement = replacement

    def __call__(self, name: str, ctx: FileContext) -> str:
        return self.rx.sub(lambda m: self.replacement, name)  # lambda: no \1 handling


class NumberingStep:
    def __init__(self, f: Filter):
        self.f = f

    def __call__(self, name: str, ctx: FileContext) -> str:
        f = self.f
        idx, count = ((ctx.folder_index, ctx.folder_count) if f.per_folder
                      else (ctx.index, ctx.count))
        n = f.start + idx * f.step
        last = f.start + (count - 1) * f.step
        width = f.digits or len(str(max(abs(f.start), abs(last))))
        number = f"{n:0{width}d}"
        return TOKEN_RE.sub(lambda m: name if m.group(1) == "name" else number, f.template)


def build_steps(filters: list[Filter]):
    """Return (steps, errors). errors = {filter_index: message}."""
    steps, errors = [], {}
    for i, f in enumerate(filters):
        if not f.enabled:
            continue
        if f.kind == "text":
            if not f.find:
                continue
            steps.append(TextStep(f.find, f.replace_with, f.ignore_case))
        elif f.kind == "regex":
            if not f.pattern:
                continue
            try:
                rx = re.compile(f.pattern, re.IGNORECASE if f.ignore_case else 0)
            except re.error as e:
                errors[i] = f"Invalid regex: {e}"
                continue
            steps.append(RegexStep(rx, f.replacement))
        elif f.kind == "numbering":
            if "{n}" not in f.template:
                errors[i] = "Template must contain {n}"
                continue
            steps.append(NumberingStep(f))
    return steps, errors


def compute_new_name(name_part: str, steps, ctx: FileContext) -> tuple[str, str | None]:
    for step in steps:
        try:
            name_part = step(name_part, ctx)
        except (re.error, IndexError) as e:
            return name_part, f"Bad replacement: {e}"
    return name_part, None


_REGEX_META = re.compile(r"([\\.^$*+?{}\[\]|()])")


def escape_regex(text: str) -> str:
    """Backslash-escape regex metacharacters (| . ( ) [ ] etc.) so `text` matches
    itself literally. Unlike re.escape this leaves spaces and punctuation alone,
    which keeps pasted patterns readable. Lookalikes such as the fullwidth bar
    (U+FF5C) are not special and are left as they are."""
    return _REGEX_META.sub(r"\\\1", text)


def validate_name(name: str) -> str | None:
    if not name or name in (".", ".."):
        return "Empty name"
    if any(c in ILLEGAL_CHARS or ord(c) < 32 for c in name):
        return "Illegal character"
    if os.name == "nt" and (name.endswith(" ") or name.endswith(".")):
        return "Cannot end with space or dot"
    return None


def natural_key(path: Path):
    parts = re.split(r"(\d+)", path.name)
    return (str(path.parent).casefold(),
            [int(t) if t.isdigit() else t.casefold() for t in parts])


def safe_rename(jobs: list[tuple[Path, Path]]) -> None:
    """Rename every (src, dst) pair using a two-phase move through temp names,
    so chains/swaps work. Rolls everything back if anything fails."""
    staged: list[tuple[Path, Path]] = []    # (original, temp)
    finished: list[tuple[Path, Path]] = []  # (original, final)
    try:
        for src, _ in jobs:
            tmp = src.with_name(f".{uuid.uuid4().hex}.tmp")
            src.rename(tmp)
            staged.append((src, tmp))
        for (src, tmp), (_, dst) in zip(staged, jobs):
            tmp.rename(dst)
            finished.append((src, dst))
    except Exception:
        done = {s for s, _ in finished}
        for src, dst in finished:
            try:
                dst.rename(src)
            except OSError:
                pass
        for src, tmp in staged:
            if src not in done:
                try:
                    tmp.rename(src)
                except OSError:
                    pass
        raise


# --------------------------------------------------------------------------
# Presets (UI-free): the filter stack saved as a small JSON file
# --------------------------------------------------------------------------
PRESET_FORMAT = "jabr-preset"
PRESET_VERSION = 1


class PresetError(Exception):
    pass


_PRESET_FIELDS = (
    ("pattern", str), ("replacement", str), ("template", str),
    ("find", str), ("replace_with", str),
    ("enabled", bool), ("ignore_case", bool), ("per_folder", bool),
    ("start", int), ("step", int), ("digits", int),
)


def _filter_to_dict(f: Filter) -> dict:
    """Only store the fields that matter for the filter's type."""
    d = {"kind": f.kind, "enabled": f.enabled}
    if f.kind == "regex":
        d.update(pattern=f.pattern, replacement=f.replacement, ignore_case=f.ignore_case)
    elif f.kind == "text":
        d.update(find=f.find, replace_with=f.replace_with, ignore_case=f.ignore_case)
    else:
        d.update(template=f.template, start=f.start, step=f.step,
                 digits=f.digits, per_folder=f.per_folder)
    return d


def _filter_from_dict(d) -> Filter:
    if not isinstance(d, dict):
        raise PresetError("A filter entry is not an object.")
    kind = d.get("kind", "regex")
    if kind not in ("regex", "text", "numbering"):
        raise PresetError(f"Unknown filter type: {kind!r}")
    f = Filter(kind=kind)
    for key, typ in _PRESET_FIELDS:
        if key not in d:
            continue
        v = d[key]
        if (typ is int and isinstance(v, bool)) or not isinstance(v, typ):
            raise PresetError(f"Bad value for '{key}': expected {typ.__name__}.")
        setattr(f, key, v)
    f.start = max(-999999, min(999999, f.start))
    f.step = max(-9999, min(9999, f.step))
    f.digits = max(0, min(12, f.digits))
    return f


def preset_to_dict(filters: list[Filter], include_ext: bool) -> dict:
    return {
        "format": PRESET_FORMAT,
        "version": PRESET_VERSION,
        "include_extension": include_ext,
        "filters": [_filter_to_dict(f) for f in filters],
    }


def preset_from_dict(data) -> tuple[list[Filter], bool | None]:
    """Validate and convert. Returns (filters, include_ext or None if not stored)."""
    if not isinstance(data, dict) or data.get("format") != PRESET_FORMAT:
        raise PresetError("This file is not a JABR preset.")
    version = data.get("version", 1)
    if not isinstance(version, int) or version > PRESET_VERSION:
        raise PresetError("This preset was made by a newer version of the app.")
    raw = data.get("filters")
    if not isinstance(raw, list):
        raise PresetError("The preset has no filter list.")
    include_ext = data.get("include_extension")
    if include_ext is not None and not isinstance(include_ext, bool):
        raise PresetError("Bad value for 'include_extension'.")
    return [_filter_from_dict(d) for d in raw], include_ext


def save_preset(path: Path, filters: list[Filter], include_ext: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(preset_to_dict(filters, include_ext), indent=2, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")


def load_preset(path: Path) -> tuple[list[Filter], bool | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise PresetError(f"Could not read the file: {e.strerror or e}") from e
    except ValueError as e:
        raise PresetError("The file is not valid JSON.") from e
    return preset_from_dict(data)


def default_presets_dir() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
    return Path(base) / "presets"


# --------------------------------------------------------------------------
# Undo history (UI-free)
# --------------------------------------------------------------------------
@dataclass
class Batch:
    when: str                           # human-readable timestamp
    pairs: list[tuple[str, str]]        # (original path, new path)


class UndoError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class History:
    """Stack of rename batches, persisted as JSON so undo survives a restart."""
    MAX = 50

    def __init__(self, path: Path | None = None):
        self.path = path
        self.batches: list[Batch] = []
        self._load()

    def _load(self):
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.batches = [Batch(b["when"], [tuple(p) for p in b["pairs"]]) for b in data]
        except (OSError, ValueError, KeyError, TypeError):
            self.batches = []  # unreadable history is ignored, never fatal

    def _save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = [{"when": b.when, "pairs": b.pairs} for b in self.batches]
            self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError:
            pass

    def push(self, jobs: list[tuple[Path, Path]]):
        when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.batches.append(Batch(when, [(str(s), str(d)) for s, d in jobs]))
        del self.batches[:-self.MAX]
        self._save()

    def peek(self) -> Batch | None:
        return self.batches[-1] if self.batches else None

    def pop(self) -> Batch | None:
        batch = self.batches.pop() if self.batches else None
        self._save()
        return batch


def check_undo(batch: Batch) -> list[str]:
    """Return a list of reasons the batch can't be undone (empty = safe)."""
    currents = {str(Path(dst)).casefold() for _, dst in batch.pairs}
    problems = []
    for src, dst in batch.pairs:
        if not Path(dst).exists():
            problems.append(f"Missing (moved or deleted?): {dst}")
        elif Path(src).exists() and str(Path(src)).casefold() not in currents:
            problems.append(f"Original name is now taken: {src}")
    return problems


def undo_batch(batch: Batch) -> None:
    """Restore the original names. Checks everything first, so it either
    undoes the whole batch or changes nothing."""
    problems = check_undo(batch)
    if problems:
        raise UndoError(problems)
    safe_rename([(Path(dst), Path(src)) for src, dst in batch.pairs])


def default_history_path() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
    return Path(base) / "history.json"


# --------------------------------------------------------------------------
# File table model
# --------------------------------------------------------------------------
class FileModel(QAbstractTableModel):
    HEADERS = ["Original name", "New name"]

    def __init__(self):
        super().__init__()
        self.files: list[Path] = []
        self.new_names: list[str] = []
        self.errors: list[str | None] = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.files)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 2

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r, c = index.row(), index.column()
        p, new, err = self.files[r], self.new_names[r], self.errors[r]
        changed = new != p.name
        if role == Qt.DisplayRole:
            if c == 0:
                return p.name
            return new
        if role == Qt.ForegroundRole:
            if err:
                return QColor("#d33")
            if c == 1 and changed:
                return QColor("#2a9d4a")
            if not changed:
                return QColor("#888")
        if role == Qt.ToolTipRole:
            return f"{err}\n{p}" if err else str(p)

    def flags(self, index):
        base = super().flags(index)
        if not index.isValid():
            return base
        flags = base | Qt.ItemIsDragEnabled
        if index.column() == 0:
            flags |= Qt.ItemIsEditable   # opens the read-only, selectable editor
        return flags

    # -- Mutation
    def move_rows(self, rows: list[int], target: int) -> tuple[int, int]:
        """Move `rows` so they sit together at insertion point `target`
        (an index into the current list, 0...len). Returns (new_start, count),
        or (-1, 0) if nothing moved."""
        n = len(self.files)
        picked = sorted({r for r in rows if 0 <= r < n})
        if not picked:
            return -1, 0
        target = max(0, min(target, n))
        insert_at = target - sum(1 for r in picked if r < target)
        pset = set(picked)
        moved = [self.files[r] for r in picked]
        rest = [p for i, p in enumerate(self.files) if i not in pset]
        self.beginResetModel()
        self.files = rest[:insert_at] + moved + rest[insert_at:]
        self.new_names = [p.name for p in self.files]
        self.errors = [None] * len(self.files)
        self.endResetModel()
        return insert_at, len(picked)

    def add_paths(self, paths: list[Path]) -> int:
        existing = {str(p) for p in self.files}
        fresh = []
        for p in paths:
            if p.is_dir():
                fresh += sorted((c for c in p.iterdir() if c.is_file()), key=natural_key)
            elif p.is_file():
                fresh.append(p)
        fresh = [p.resolve() for p in fresh]
        fresh = [p for p in dict.fromkeys(fresh) if str(p) not in existing]
        if not fresh:
            return 0
        start = len(self.files)
        self.beginInsertRows(QModelIndex(), start, start + len(fresh) - 1)
        self.files += fresh
        self.new_names += [p.name for p in fresh]
        self.errors += [None] * len(fresh)
        self.endInsertRows()
        return len(fresh)

    def remove_rows(self, rows: list[int]):
        for r in sorted(set(rows), reverse=True):
            self.beginRemoveRows(QModelIndex(), r, r)
            del self.files[r], self.new_names[r], self.errors[r]
            self.endRemoveRows()

    def clear(self):
        self.beginResetModel()
        self.files, self.new_names, self.errors = [], [], []
        self.endResetModel()

    def sort_natural(self):
        self.beginResetModel()
        self.files.sort(key=natural_key)
        self.new_names = [p.name for p in self.files]
        self.errors = [None] * len(self.files)
        self.endResetModel()

    # -- Preview computation
    def recompute(self, filters: list[Filter], include_ext: bool):
        steps, _ = build_steps(filters)
        sources = {str(p).casefold() for p in self.files}

        folder_total = Counter(p.parent for p in self.files)
        folder_seen: Counter = Counter()
        total = len(self.files)

        news, errs = [], []
        for i, p in enumerate(self.files):
            ctx = FileContext(i, total, folder_seen[p.parent], folder_total[p.parent])
            folder_seen[p.parent] += 1
            base, ext = (p.name, "") if include_ext else (p.stem, p.suffix)
            new, err = compute_new_name(base, steps, ctx)
            new += ext
            errs.append(err or (validate_name(new) if new != p.name else None))
            news.append(new)

        # Duplicate targets within the same folder (case-insensitive = conservative)
        seen: dict[tuple[str, str], int] = {}
        for p, n in zip(self.files, news):
            key = (str(p.parent).casefold(), n.casefold())
            seen[key] = seen.get(key, 0) + 1
        for i, (p, n) in enumerate(zip(self.files, news)):
            if errs[i] or n == p.name:
                continue
            if seen[(str(p.parent).casefold(), n.casefold())] > 1:
                errs[i] = "Duplicate target name"
            else:
                target = p.with_name(n)
                if target.exists() and str(target).casefold() not in sources:
                    errs[i] = "Target already exists"

        self.new_names, self.errors = news, errs
        if self.files:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.files) - 1, 1))

    def replace_paths(self, mapping: dict[str, Path]):
        """Point listed files at new paths (used after an undo)."""
        self.beginResetModel()
        self.files = [mapping.get(str(p), p) for p in self.files]
        self.new_names = [p.name for p in self.files]
        self.errors = [None] * len(self.files)
        self.endResetModel()

    def context_for(self, row: int) -> FileContext:
        """Where one file sits in the list, for numbering filters."""
        p = self.files[row]
        return FileContext(row, len(self.files),
                           sum(1 for q in self.files[:row] if q.parent == p.parent),
                           sum(1 for q in self.files if q.parent == p.parent))

    def preview_name(self, row: int, filters: list[Filter], include_ext: bool):
        """The name file `row` would get from just these filters (used by the live
        preview). Returns (name, error_or_None). Does not touch new_names."""
        steps, _ = build_steps(filters)
        p = self.files[row]
        base, ext = (p.name, "") if include_ext else (p.stem, p.suffix)
        new, err = compute_new_name(base, steps, self.context_for(row))
        return new + ext, err

    def jobs(self) -> list[tuple[Path, Path]]:
        return [(p, p.with_name(n)) for p, n in zip(self.files, self.new_names) if n != p.name]

    def apply_renames(self):
        self.files = [p.with_name(n) for p, n in zip(self.files, self.new_names)]
        self.errors = [None] * len(self.files)
        if self.files:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.files) - 1, 1))


class CopyableLineEdit(QLineEdit):
    """Read-only text box: select any part of the text and copy it (Ctrl+C).
    Right-click, or Ctrl+Shift+C, also offers "Copy as regex (escaped)" so a
    selected | or ( pastes into a pattern as a literal character )."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)

    def copy_escaped(self):
        text = self.selectedText()
        if text:
            QApplication.clipboard().setText(escape_regex(text))

    def build_context_menu(self):
        menu = self.createStandardContextMenu()
        menu.addSeparator()
        act = menu.addAction("Copy as regex (escaped)\tCtrl+Shift+C")
        act.setEnabled(self.hasSelectedText())
        act.triggered.connect(self.copy_escaped)
        return menu

    def contextMenuEvent(self, e):
        self.build_context_menu().exec(e.globalPos())

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_C and e.modifiers() == (Qt.ControlModifier | Qt.ShiftModifier):
            self.copy_escaped()
            return
        super().keyPressEvent(e)


class SelectableNameDelegate(QStyledItemDelegate):
    """Double-click (or F2 on) an original name to open it as selectable text."""

    def createEditor(self, parent, option, index):
        return CopyableLineEdit(parent)

    def setEditorData(self, editor, index):
        editor.setText(str(index.data(Qt.DisplayRole) or ""))
        editor.setCursorPosition(0)

    def setModelData(self, editor, model, index):
        pass  # read-only: never writes anything back

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)


class FileTable(QTableView):
    """File list. Accepts files/folders dragged in from the OS, and lets you
    drag selected rows to reorder them (a line shows where they will land)."""
    paths_dropped = Signal(list)
    rows_moved = Signal(list, int)   # (row numbers, insertion index)

    def __init__(self, model):
        super().__init__()
        self.setModel(model)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDropIndicatorShown(False)  # we paint our own line
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.setItemDelegateForColumn(0, SelectableNameDelegate(self))
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.horizontalHeader().setStretchLastSection(True)
        self.setColumnWidth(0, 670)
        #self.setColumnWidth(1, 260)
        self._drop_row: int | None = None

    # -- helpers
    def selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.selectionModel().selectedRows()})

    def select_rows(self, start: int, count: int):
        sm, m = self.selectionModel(), self.model()
        sm.clearSelection()
        for r in range(start, start + count):
            sm.select(m.index(r, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
        self.scrollTo(m.index(start, 0))

    def _row_at(self, pos) -> int:
        """Insertion index for a drag at viewport position `pos`."""
        n = self.model().rowCount()
        idx = self.indexAt(pos)
        if not idx.isValid():
            return n
        below_middle = pos.y() > self.visualRect(idx).center().y()
        return idx.row() + (1 if below_middle else 0)

    def _clear_indicator(self):
        if self._drop_row is not None:
            self._drop_row = None
            self.viewport().update()

    # -- starting a drag (we do it ourselves so Qt never deletes the source rows)
    def startDrag(self, supported_actions):
        rows = self.selected_rows()
        if not rows:
            return
        mime = QMimeData()
        mime.setData(MIME_ROWS, ",".join(map(str, rows)).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        tag = QLabel(f"  {len(rows)} file{'s' if len(rows) != 1 else ''}  ")
        tag.setStyleSheet("background: palette(highlight); color: palette(highlighted-text);"
                          "border-radius: 4px; padding: 3px;")
        tag.adjustSize()
        drag.setPixmap(tag.grab())
        drag.exec(Qt.MoveAction)
        self._clear_indicator()

    # -- receiving drags
    def dragEnterEvent(self, e):
        md = e.mimeData()
        if md.hasFormat(MIME_ROWS):
            e.setDropAction(Qt.MoveAction)
            e.accept()
        elif md.hasUrls():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        md = e.mimeData()
        if md.hasFormat(MIME_ROWS):
            pos = e.position().toPoint()
            e.setDropAction(Qt.MoveAction)
            e.accept()
            row = self._row_at(pos)
            if row != self._drop_row:
                self._drop_row = row
                self.viewport().update()
            # crude auto-scroll near the top/bottom edge
            bar, margin = self.verticalScrollBar(), 24
            if pos.y() < margin:
                bar.setValue(bar.value() - bar.singleStep())
            elif pos.y() > self.viewport().height() - margin:
                bar.setValue(bar.value() + bar.singleStep())
        elif md.hasUrls():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self._clear_indicator()
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        md = e.mimeData()
        if md.hasFormat(MIME_ROWS):
            target = self._row_at(e.position().toPoint())
            raw = bytes(md.data(MIME_ROWS)).decode()
            rows = [int(x) for x in raw.split(",") if x.strip().isdigit()]
            self._clear_indicator()
            e.setDropAction(Qt.MoveAction)
            e.accept()
            self.rows_moved.emit(rows, target)
        elif md.hasUrls():
            paths = [Path(u.toLocalFile()) for u in md.urls() if u.isLocalFile()]
            if paths:
                self.paths_dropped.emit(paths)
                e.acceptProposedAction()

    def paintEvent(self, e):
        super().paintEvent(e)
        n = self.model().rowCount()
        if self._drop_row is None or n == 0:
            return
        m = self.model()
        if self._drop_row < n:
            y = self.visualRect(m.index(self._drop_row, 0)).top()
        else:
            y = self.visualRect(m.index(n - 1, 0)).bottom()
        p = QPainter(self.viewport())
        p.setPen(QPen(self.palette().highlight().color(), 2))
        p.drawLine(0, y, self.viewport().width(), y)
        p.end()


# --------------------------------------------------------------------------
# Filter stack editor: list of filters on top, editor for the selected one below
# --------------------------------------------------------------------------
class FilterPanel(QWidget):
    changed = Signal()
    selection_changed = Signal()   # a different filter was selected (or none)

    def __init__(self):
        super().__init__()
        self.filters: list[Filter] = []   # starts empty; filters are added from the Add menu
        self._loading = False

        # -- filter list
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["On", "Type", "Summary"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.itemChanged.connect(self._check_changed)
        self.table.itemSelectionChanged.connect(self._sync_editor)

        add_btn = QPushButton("Add \u25be")
        menu = QMenu(add_btn)
        menu.addAction("Text replace", lambda: self.add("text"))
        menu.addAction("Regex replace", lambda: self.add("regex"))
        menu.addAction("Numbering", lambda: self.add("numbering"))
        add_btn.setMenu(menu)
        buttons = QHBoxLayout()
        buttons.addWidget(add_btn)
        for text, slot in [("Remove", self.remove),
                           ("Up", lambda: self.move(-1)), ("Down", lambda: self.move(1))]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        buttons.addStretch()

        # -- text (plain find & replace) editor page
        self.text_find_edit = QLineEdit()
        self.text_find_edit.setPlaceholderText("text to find, e.g.  \uff5c   or   [720p]")
        self.text_repl_edit = QLineEdit()
        self.text_repl_edit.setPlaceholderText("leave empty to delete the text")
        self.text_case_box = QCheckBox("Ignore case")
        text_page = QWidget()
        tf = QFormLayout(text_page)
        tf.setContentsMargins(0, 0, 0, 0)
        tf.addRow("Find:", self.text_find_edit)
        tf.addRow("Replace with:", self.text_repl_edit)
        tf.addRow("", self.text_case_box)
        tf.addRow(self._hint("Plain text, not a regex: every occurrence is replaced, and "
                             "characters like | . ( ) \\ mean just themselves."))

        # -- regex editor page
        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText(r"e.g.  IMG[_ ](\d+)")
        self.repl_edit = QLineEdit()
        self.repl_edit.setPlaceholderText(r"e.g.  Holiday_\1")
        self.case_box = QCheckBox("Ignore case")
        regex_page = QWidget()
        rf = QFormLayout(regex_page)
        rf.setContentsMargins(0, 0, 0, 0)
        rf.addRow("Find (regex):", self.find_edit)
        rf.addRow("Replace with:", self.repl_edit)
        rf.addRow("", self.case_box)
        rf.addRow(self._hint("Use \\1 or \\g<name> for groups. ^ and $ anchor the name."))

        # -- numbering editor page
        self.template_edit = QLineEdit()
        self.start_spin = QSpinBox()
        self.start_spin.setRange(-999999, 999999)
        self.step_spin = QSpinBox()
        self.step_spin.setRange(-9999, 9999)
        self.digits_spin = QSpinBox()
        self.digits_spin.setRange(0, 12)
        self.digits_spin.setSpecialValueText("Auto")
        self.folder_box = QCheckBox("Restart numbering in each folder")
        num_page = QWidget()
        nf = QFormLayout(num_page)
        nf.setContentsMargins(0, 0, 0, 0)
        nf.addRow("Template:", self.template_edit)
        nf.addRow("Start at:", self.start_spin)
        nf.addRow("Step:", self.step_spin)
        nf.addRow("Digits:", self.digits_spin)
        nf.addRow("", self.folder_box)
        nf.addRow(self._hint("{n} = the number, {name} = the current name. "
                             "{name}_{n} adds a suffix, {n}_{name} a prefix, "
                             "Holiday_{n} replaces the name. Auto digits pads to fit "
                             "the largest number. Numbers follow list order."))

        self.stack = QStackedWidget()
        self.stack.addWidget(regex_page)          # 0
        self.stack.addWidget(num_page)            # 1
        self.stack.addWidget(QLabel("No filter selected. Use Add \u25be to create one."))  # 2
        self.stack.addWidget(text_page)           # 3

        self.err_label = QLabel()
        self.err_label.setStyleSheet("color: #d33;")
        self.err_label.setWordWrap(True)

        for w in (self.find_edit, self.repl_edit, self.template_edit,
                  self.text_find_edit, self.text_repl_edit):
            w.textEdited.connect(self._edited)
        for w in (self.case_box, self.folder_box, self.text_case_box):
            w.toggled.connect(self._edited)
        for w in (self.start_spin, self.step_spin, self.digits_spin):
            w.valueChanged.connect(self._edited)

        # Wide-and-short layout (the panel sits below the file list):
        # filter list on the left, editor for the selected filter on the right.
        left = QVBoxLayout()
        self.header = QHBoxLayout()
        self.header.addWidget(QLabel("<b>Filters</b> (applied top to bottom)"))
        self.header.addStretch()
        left.addLayout(self.header)
        left.addWidget(self.table, 1)
        left.addLayout(buttons)

        right = QVBoxLayout()
        right.addSpacing(self.header.sizeHint().height())  # line up with the list
        right.addWidget(self.stack)
        right.addWidget(self.err_label)
        right.addStretch(1)

        columns = QHBoxLayout()
        columns.setSpacing(14)
        columns.addLayout(left, 3)
        columns.addLayout(right, 2)

        # Live preview strip: one file's name before and after the filter stack. With a
        # filter selected, "before" is the name coming into that filter and "after" is the
        # name coming out of it (filters below it are ignored).
        self.preview_box = QGroupBox("Live preview")
        self.preview_orig = CopyableLineEdit()
        self.preview_new = QLineEdit()
        self.preview_new.setReadOnly(True)   # selectable/copyable, but not editable
        self._orig_tip = ("Select text and press Ctrl+C to copy it.\n"
                          "Right-click or Ctrl+Shift+C: copy escaped for a regex (| becomes \\|).")
        self.preview_orig.setToolTip(self._orig_tip)
        self.preview_orig.setPlaceholderText("(add files to see a preview)")
        self.preview_new.setPlaceholderText("(add files to see a preview)")
        grid = QGridLayout(self.preview_box)
        self.preview_orig_label = QLabel("Original file name:")
        self.preview_new_label = QLabel("Modified file name:")
        # Labels sit in a row ABOVE the boxes. Their width is ignored by the layout, so
        # changing the label text (e.g. "Before filter 10:") can never move or resize
        # the boxes; both boxes always share the width equally.
        for lbl in (self.preview_orig_label, self.preview_new_label):
            lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        arrow = QLabel("\u2192")
        arrow.setAlignment(Qt.AlignCenter)
        grid.addWidget(self.preview_orig_label, 0, 0)
        grid.addWidget(self.preview_new_label, 0, 2)
        grid.addWidget(self.preview_orig, 1, 0)
        grid.addWidget(arrow, 1, 1)
        grid.addWidget(self.preview_new, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.preview_box)
        layout.addLayout(columns, 1)
        self.rebuild()

    @staticmethod
    def _hint(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: #888;")
        return lbl

    # -- list <-> data
    @staticmethod
    def _summary(f: Filter) -> str:
        if f.kind == "text":
            if not f.find:
                return "(empty find)"
            return f'"{f.find}" \u2192 "{f.replace_with}"' + ("  (ignore case)" if f.ignore_case else "")
        if f.kind == "regex":
            if not f.pattern:
                return "(empty regex)"
            return f"s/{f.pattern}/{f.replacement}/{'i' if f.ignore_case else ''}"
        digits = "auto digits" if f.digits == 0 else f"{f.digits} digits"
        scope = ", per folder" if f.per_folder else ""
        return f"{f.template}  [start {f.start}, step {f.step}, {digits}{scope}]"

    def rebuild(self, select: int | None = None):
        self._loading = True
        self.table.setRowCount(len(self.filters))
        for r, f in enumerate(self.filters):
            on = QTableWidgetItem()
            on.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            on.setCheckState(Qt.Checked if f.enabled else Qt.Unchecked)
            kind = QTableWidgetItem({"regex": "Regex", "text": "Text"}.get(f.kind, "Numbering"))
            summary = QTableWidgetItem()
            for c, item in enumerate((on, kind, summary)):
                self.table.setItem(r, c, item)
        self._loading = False
        self._refresh_rows()
        if select is not None and 0 <= select < len(self.filters):
            self.table.selectRow(select)
        self._sync_editor()

    def _refresh_rows(self):
        """Update summaries and error highlighting without touching selection."""
        _, errors = build_steps(self.filters)
        was = self._loading
        self._loading = True
        for r, f in enumerate(self.filters):
            item = self.table.item(r, 2)
            if item is None:
                continue
            item.setText(self._summary(f))
            if r in errors:
                item.setForeground(QColor("#d33"))
                item.setToolTip(errors[r])
            else:
                item.setData(Qt.ForegroundRole, None)
                item.setToolTip("")
        self._loading = was
        row = self.table.currentRow()
        self.err_label.setText(errors.get(row, "") if row >= 0 else "")

    def _sync_editor(self):
        if self._loading:
            return
        row = self.table.currentRow()
        if row < 0 or row >= len(self.filters):
            self.stack.setCurrentIndex(2)
            self.err_label.setText("")
            self.selection_changed.emit()
            return
        f = self.filters[row]
        self._loading = True
        if f.kind == "text":
            self.stack.setCurrentIndex(3)
            self.text_find_edit.setText(f.find)
            self.text_repl_edit.setText(f.replace_with)
            self.text_case_box.setChecked(f.ignore_case)
        elif f.kind == "regex":
            self.stack.setCurrentIndex(0)
            self.find_edit.setText(f.pattern)
            self.repl_edit.setText(f.replacement)
            self.case_box.setChecked(f.ignore_case)
        else:
            self.stack.setCurrentIndex(1)
            self.template_edit.setText(f.template)
            self.start_spin.setValue(f.start)
            self.step_spin.setValue(f.step)
            self.digits_spin.setValue(f.digits)
            self.folder_box.setChecked(f.per_folder)
        self._loading = False
        self._refresh_rows()
        self.selection_changed.emit()

    def selected_filter_index(self) -> int:
        """Row of the selected filter, or -1 when none is selected."""
        rows = self.table.selectionModel().selectedRows()
        row = rows[0].row() if rows else -1
        return row if 0 <= row < len(self.filters) else -1

    # -- edits
    def _edited(self, *_):
        if self._loading:
            return
        row = self.table.currentRow()
        if row < 0:
            return
        f = self.filters[row]
        if f.kind == "text":
            f.find = self.text_find_edit.text()
            f.replace_with = self.text_repl_edit.text()
            f.ignore_case = self.text_case_box.isChecked()
        elif f.kind == "regex":
            f.pattern = self.find_edit.text()
            f.replacement = self.repl_edit.text()
            f.ignore_case = self.case_box.isChecked()
        else:
            f.template = self.template_edit.text()
            f.start = self.start_spin.value()
            f.step = self.step_spin.value()
            f.digits = self.digits_spin.value()
            f.per_folder = self.folder_box.isChecked()
        self._refresh_rows()
        self.changed.emit()

    def _check_changed(self, item: QTableWidgetItem):
        if self._loading or item.column() != 0:
            return
        self.filters[item.row()].enabled = item.checkState() == Qt.Checked
        self._refresh_rows()
        self.changed.emit()

    PREVIEW_COLORS = {"changed": "#2a9d4a", "same": "#888", "error": "#d33", "empty": ""}

    def set_preview(self, title: str, original: str, modified: str,
                    state: str = "empty", error: str = "",
                    left_label: str = "Original file name:",
                    right_label: str = "Modified file name:", left_tip: str = ""):
        """state: 'changed' (green), 'same' (gray), 'error' (red) or 'empty'."""
        self.preview_box.setTitle(title)
        self.preview_orig_label.setText(left_label)
        self.preview_new_label.setText(right_label)
        self.preview_orig.setToolTip(f"{left_tip}\n\n{self._orig_tip}" if left_tip else self._orig_tip)
        self.preview_orig.setText(original)
        self.preview_new.setText(modified)
        self.preview_orig.setCursorPosition(0)
        self.preview_new.setCursorPosition(0)
        color = self.PREVIEW_COLORS.get(state, "")
        self.preview_new.setStyleSheet(f"color: {color};" if color else "")
        self.preview_new.setToolTip(error)

    def has_content(self) -> bool:
        """False when the stack is empty or holds only blank regex filters (which do nothing)."""
        return any(f.kind != "regex" or f.pattern or f.replacement for f in self.filters)

    def set_filters(self, filters: list[Filter], append: bool = False):
        if append:
            # drop blank regex filters (no-ops) so they don't sit above the preset
            base = [f for f in self.filters
                    if f.kind != "regex" or f.pattern or f.replacement]
            self.filters = base + list(filters)
        else:
            self.filters = list(filters)
        self.rebuild(select=0 if self.filters else None)
        self.changed.emit()

    def add(self, kind: str = "regex"):
        self.filters.append(Filter(kind=kind))
        self.rebuild(select=len(self.filters) - 1)
        {"text": self.text_find_edit, "regex": self.find_edit}.get(kind, self.template_edit).setFocus()
        self.changed.emit()

    def remove(self):
        r = self.table.currentRow()
        if r >= 0:
            del self.filters[r]
            self.rebuild(select=min(r, len(self.filters) - 1))
            self.changed.emit()

    def move(self, delta: int):
        r = self.table.currentRow()
        t = r + delta
        if r >= 0 and 0 <= t < len(self.filters):
            self.filters[r], self.filters[t] = self.filters[t], self.filters[r]
            self.rebuild(select=t)
            self.changed.emit()


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, history: History | None = None, presets_dir: Path | None = None):
        super().__init__()
        self.presets_dir = presets_dir if presets_dir is not None else default_presets_dir()
        self.history = history if history is not None else History(default_history_path())
        self.setWindowTitle("JABR")
        self.resize(1400, 820)

        self.model = FileModel()
        self.table = FileTable(self.model)
        self.table.paths_dropped.connect(self.add_paths)
        self.table.rows_moved.connect(self.move_rows)
        self.table.selectionModel().selectionChanged.connect(self.update_preview)
        self.panel = FilterPanel()
        self.panel.changed.connect(self.refresh)
        self.panel.selection_changed.connect(self.update_preview)

        self.include_ext = QCheckBox("Apply filters to extension too")
        self.include_ext.toggled.connect(self.refresh)

        self.presets_btn = QPushButton("Presets \u25be")
        self.presets_menu = QMenu(self.presets_btn)
        self.presets_menu.aboutToShow.connect(self._fill_presets_menu)
        self.presets_btn.setMenu(self.presets_menu)
        self.panel.header.addWidget(self.presets_btn)

        add_btn = QPushButton("Add files...")
        add_btn.clicked.connect(self.pick_files)
        rm_btn = QPushButton("Remove selected")
        rm_btn.clicked.connect(self.remove_selected)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.clear)
        up_btn = QPushButton("\u25b2 Up")
        up_btn.setToolTip("Move selected files up (Alt+Up)")
        up_btn.clicked.connect(lambda: self.nudge(-1))
        down_btn = QPushButton("\u25bc Down")
        down_btn.setToolTip("Move selected files down (Alt+Down)")
        down_btn.clicked.connect(lambda: self.nudge(1))
        sort_btn = QPushButton("Sort A\u2192Z")
        sort_btn.setToolTip("Natural sort (file2 before file10). Numbering follows list order.")
        sort_btn.clicked.connect(self.sort_list)
        self.undo_btn = QPushButton("Undo last rename")
        self.undo_btn.clicked.connect(self.undo_last)
        self.rename_btn = QPushButton("Rename files")
        self.rename_btn.setStyleSheet("font-weight: bold;")
        self.rename_btn.clicked.connect(self.do_rename)

        toolbar = QHBoxLayout()
        for w in (add_btn, rm_btn, clear_btn, up_btn, down_btn, sort_btn):
            toolbar.addWidget(w)
        toolbar.addStretch()
        toolbar.addWidget(self.include_ext)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addLayout(toolbar)
        lv.addWidget(self.table)
        self.drop_hint = QLabel("Drag files or folders onto the list. Drag rows to reorder them.")
        self.drop_hint.setStyleSheet("color: #888;")
        lv.addWidget(self.drop_hint)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(left)
        splitter.addWidget(self.panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([400, 330])
        splitter.setChildrenCollapsible(False)

        bottom = QHBoxLayout()
        self.status = QLabel()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.undo_btn)
        bottom.addWidget(self.rename_btn)

        central = QWidget()
        cv = QVBoxLayout(central)
        cv.addWidget(splitter, 1)
        cv.addLayout(bottom)
        self.setCentralWidget(central)

        delete_sc = QShortcut(QKeySequence.Delete, self.table, activated=self.remove_selected)
        delete_sc.setContext(Qt.WidgetWithChildrenShortcut)   # not while typing in the filter panel
        copy_sc = QShortcut(QKeySequence.Copy, self.table, activated=self.copy_selected_names)
        copy_sc.setContext(Qt.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence.Undo, self, activated=self.undo_last)
        QShortcut(QKeySequence("Alt+Up"), self, activated=lambda: self.nudge(-1))
        QShortcut(QKeySequence("Alt+Down"), self, activated=lambda: self.nudge(1))
        self.refresh()
        self.update_undo_button()

    def add_paths(self, paths):
        self.model.add_paths([Path(p) for p in paths])
        self.refresh()

    def pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Add files")
        if files:
            self.add_paths(files)

    def remove_selected(self):
        rows = [i.row() for i in self.table.selectionModel().selectedRows()]
        self.model.remove_rows(rows)
        self.refresh()

    def clear(self):
        self.model.clear()
        self.refresh()

    def move_rows(self, rows, target):
        start, count = self.model.move_rows(rows, target)
        if start < 0:
            return
        self.refresh()
        self.table.select_rows(start, count)

    def nudge(self, delta: int):
        rows = self.table.selected_rows()
        if not rows:
            return
        target = rows[0] - 1 if delta < 0 else rows[-1] + 2
        self.move_rows(rows, max(0, min(target, len(self.model.files))))

    def copy_selected_names(self):
        """Ctrl+C with rows selected: copy their original names, one per line."""
        rows = self.table.selected_rows()
        if rows:
            QApplication.clipboard().setText("\n".join(self.model.files[r].name for r in rows))
            self.statusBar().showMessage(f"Copied {len(rows)} file name(s).", 3000)

    def sort_list(self):
        self.model.sort_natural()
        self.refresh()

    def refresh(self):
        self.model.recompute(self.panel.filters, self.include_ext.isChecked())
        total = len(self.model.files)
        jobs = len(self.model.jobs())
        errs = sum(1 for e in self.model.errors if e)
        text = f"{total} files  |  {jobs} will be renamed  |  {errs} problems"
        first = next(((p, e) for p, e in zip(self.model.files, self.model.errors) if e), None)
        if first:
            text += f"  (first: {first[0].name} \u2013 {first[1]})"
        self.status.setText(text)
        self.rename_btn.setEnabled(jobs > 0 and errs == 0)
        self.drop_hint.setVisible(total == 0)
        self.update_preview()

    def update_preview(self, *_):
        """Show one file before/after the filters: the first selected file,
        or the first file in the list when nothing is selected."""
        n = len(self.model.files)
        if n == 0:
            self.panel.set_preview("Live preview", "", "", "empty")
            return
        rows = self.table.selected_rows()
        if rows:
            row = rows[0]
            which = "selected file" if len(rows) == 1 else f"first of {len(rows)} selected files"
        else:
            row, which = 0, "first file in list"
        path = self.model.files[row]
        head = f"Live preview \u2013 {which} ({row + 1} of {n})"

        k = self.panel.selected_filter_index()
        if k >= 0:
            self._show_step_preview(head, row, k)
            return

        new, err = self.model.new_names[row], self.model.errors[row]
        state = "error" if err else ("same" if new == path.name else "changed")
        self.panel.set_preview(head, path.name, new, state, err or "")

    def _show_step_preview(self, head: str, row: int, k: int):
        """A filter is selected. Left: the original name, or the result of the filters
        above it. Right: the result including the selected filter. Filters below it are
        ignored, and the file list's final names are not touched."""
        filters, inc = self.panel.filters, self.include_ext.isChecked()
        f, total = filters[k], len(filters)
        path = self.model.files[row]
        before, _ = self.model.preview_name(row, filters[:k], inc)
        after, call_err = self.model.preview_name(row, filters[:k + 1], inc)
        step_err = build_steps(filters[:k + 1])[1].get(k)   # e.g. invalid regex

        blank = (f.kind == "regex" and not f.pattern) or (f.kind == "text" and not f.find)
        if step_err or call_err:
            state, tip = "error", step_err or call_err
        elif after == before:
            state = "same"
            tip = ("This filter is switched off." if not f.enabled else
                   "Nothing to do yet: this filter is empty." if blank else
                   "This filter does not change this file's name.")
        else:
            state, tip = "changed", ""

        if k == 0:
            left_label, left_tip = "Original file name:", ""
        else:
            left_label = f"Before filter {k + 1}:"
            upto = "filter 1" if k == 1 else f"filters 1\u2013{k}"
            left_tip = (f"The name after {upto}, which is what filter {k + 1} receives. "
                        f"The original name is {path.name}")
        self.panel.set_preview(f"{head} \u2013 filter {k + 1} of {total}", before, after,
                               state, tip, left_label=left_label,
                               right_label=f"After filter {k + 1}:", left_tip=left_tip)

    def do_rename(self):
        jobs = self.model.jobs()
        if not jobs:
            return
        if QMessageBox.question(self, "Rename", f"Rename {len(jobs)} file(s)?") != QMessageBox.Yes:
            return
        try:
            safe_rename(jobs)
        except Exception as e:
            QMessageBox.critical(self, "Rename failed",
                                 f"{e}\n\nAll changes were rolled back.")
            return
        self.history.push(jobs)
        self.model.apply_renames()
        self.refresh()
        self.update_undo_button()
        self.statusBar().showMessage(f"Renamed {len(jobs)} file(s). Ctrl+Z to undo.", 8000)
        self.clear()

    # -- presets
    def _fill_presets_menu(self):
        m = self.presets_menu
        m.clear()
        m.addAction("Save current filters as preset...", self.save_preset_dialog)
        m.addAction("Load preset from file...", self.load_preset_dialog)
        m.addSeparator()
        try:
            saved = sorted(self.presets_dir.glob("*.json"), key=lambda p: p.stem.casefold())
        except OSError:
            saved = []
        if saved:
            for p in saved:
                m.addAction(p.stem, lambda checked=False, p=p: self.load_preset_file(p))
        else:
            m.addAction("(no saved presets yet)").setEnabled(False)
        m.addSeparator()
        m.addAction("Open presets folder", self._open_presets_dir)

    def _open_presets_dir(self):
        try:
            self.presets_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.presets_dir)))

    def save_preset_dialog(self):
        if not self.panel.filters:
            QMessageBox.information(self, "Save preset", "Add at least one filter first.")
            return
        try:
            self.presets_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        path, _ = QFileDialog.getSaveFileName(
            self, "Save preset", str(self.presets_dir / "my preset.json"), "Presets (*.json)")
        if not path:
            return
        p = Path(path)
        if p.suffix.lower() != ".json":
            p = p.with_name(p.name + ".json")
        try:
            save_preset(p, self.panel.filters, self.include_ext.isChecked())
        except OSError as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return
        self.statusBar().showMessage(f"Saved preset: {p.name}", 6000)

    def load_preset_dialog(self):
        start = str(self.presets_dir) if self.presets_dir.exists() else ""
        path, _ = QFileDialog.getOpenFileName(self, "Load preset", start, "Presets (*.json)")
        if path:
            self.load_preset_file(Path(path))

    def load_preset_file(self, path: Path):
        try:
            filters, include_ext = load_preset(path)
        except PresetError as e:
            QMessageBox.warning(self, "Can't load preset", f"{path.name}\n\n{e}")
            return
        append = False
        if self.panel.has_content():
            box = QMessageBox(
                QMessageBox.Question, "Load preset",
                f"Replace your current filters with \"{path.stem}\", "
                "or add its filters to the end of the stack?", parent=self)
            replace_btn = box.addButton("Replace", QMessageBox.AcceptRole)
            append_btn = box.addButton("Append", QMessageBox.ActionRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked is append_btn:
                append = True
            elif clicked is not replace_btn:
                return
        if include_ext is not None and not append:
            self.include_ext.setChecked(include_ext)
        self.panel.set_filters(filters, append=append)
        n = len(filters)
        self.statusBar().showMessage(
            f"{'Appended' if append else 'Loaded'} preset \"{path.stem}\" ({n} filter{'s' if n != 1 else ''}).", 6000)

    def update_undo_button(self):
        batch = self.history.peek()
        self.undo_btn.setEnabled(batch is not None)
        self.undo_btn.setToolTip(
            f"{len(batch.pairs)} file(s) renamed on {batch.when}" if batch else "Nothing to undo")

    def undo_last(self):
        batch = self.history.peek()
        if batch is None:
            return
        n = len(batch.pairs)
        if QMessageBox.question(
                self, "Undo rename",
                f"Undo the rename of {n} file(s) made on {batch.when}?") != QMessageBox.Yes:
            return
        try:
            undo_batch(batch)
        except UndoError as e:
            shown = "\n".join(e.problems[:6]) + ("\n..." if len(e.problems) > 6 else "")
            box = QMessageBox(QMessageBox.Warning, "Can't undo",
                              f"Nothing was changed.\n\n{shown}", parent=self)
            discard = box.addButton("Discard this history entry", QMessageBox.DestructiveRole)
            box.addButton(QMessageBox.Close)
            box.exec()
            if box.clickedButton() is discard:
                self.history.pop()
                self.update_undo_button()
            return
        except Exception as e:
            QMessageBox.critical(self, "Undo failed", f"{e}\n\nChanges were rolled back.")
            return
        self.history.pop()
        self.model.replace_paths({dst: Path(src) for src, dst in batch.pairs})
        self.refresh()
        self.update_undo_button()
        self.statusBar().showMessage(f"Restored {n} file(s).", 8000)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.add_paths([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("JABR")
    app.setDesktopFileName("JABR")   # lets docks/taskbars match the .desktop launcher
    here = Path(__file__).resolve().parent
    for icon in (here / "jabr.png", here / "packaging" / "jabr.png"):
        if icon.exists():
            app.setWindowIcon(QIcon(str(icon)))
            break
    win = MainWindow()
    win.setAcceptDrops(True)
    if len(sys.argv) > 1:
        win.add_paths(sys.argv[1:])
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()