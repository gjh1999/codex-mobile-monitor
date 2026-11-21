"""Local wallpaper preferences and the desktop wallpaper manager."""
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, QSize, Qt
from PySide6.QtGui import QImageReader, QPixmap
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout,
)

from desktop_theme import ui_font
from handoff_sync import atomic_write

BUILTIN = {
    'os-x-mavericks-wave.jpg': 'Mavericks 海浪',
    'macos-big-sur-day.jpg': 'Big Sur 白昼',
    'macos-catalina-night.jpg': 'Catalina 夜景',
}
EXTENSIONS = {'.jpg', '.jpeg', '.png'}
ORIGINS = {'local': '本机图库', 'imported': '自选图片', 'builtin': '内置壁纸'}
PREVIEW_SIZE = QSize(440, 248)


@dataclass(frozen=True)
class Wallpaper:
    key: str
    path: Path
    origin: str

    @property
    def name(self):
        return BUILTIN[self.path.name] if self.origin == 'builtin' else self.path.stem


def image_reader(path):
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    return reader


def read_preview(reader):
    size = reader.size()
    if size.isValid() and (size.width() > PREVIEW_SIZE.width() or size.height() > PREVIEW_SIZE.height()):
        reader.setScaledSize(size.scaled(PREVIEW_SIZE, Qt.AspectRatioMode.KeepAspectRatio))
    return reader.read()


class WallpaperLibrary:
    def __init__(self, assets, data_root):
        self.assets = assets
        directory = data_root / '.local' / 'desktop-wallpapers'
        self.images = directory / 'images'
        self.settings = directory / 'hidden.json'
        try:
            data = json.loads(self.settings.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data = []
        self.hidden = {key for key in data if isinstance(key, str)} if isinstance(data, list) else set()

    def entries(self, local):
        entries = [Wallpaper('local/' + os.path.normcase(str(path)), path, 'local') for path in local]
        entries.extend(Wallpaper('imported/' + path.name, path, 'imported')
                       for path in sorted(self.images.glob('*')) if path.suffix.lower() in EXTENSIONS
                       and path.is_file() and image_reader(path).canRead())
        entries.extend(Wallpaper('builtin/' + name, self.assets / name, 'builtin')
                       for name in BUILTIN if (self.assets / name).is_file())
        return entries

    def set_hidden(self, key, hidden):
        updated = self.hidden | {key} if hidden else self.hidden - {key}
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.settings, json.dumps(sorted(updated), ensure_ascii=False, indent=2).encode('utf-8'))
        self.hidden = updated

    def import_image(self, source):
        suffix = source.suffix.lower()
        if suffix not in EXTENSIONS or read_preview(image_reader(source)).isNull():
            raise ValueError('请选择可读取的 JPG 或 PNG 图片')
        self.images.mkdir(parents=True, exist_ok=True)
        if source.resolve().parent == self.images.resolve():
            return 'imported/' + source.name
        with source.open('rb') as original:
            number = 1
            while True:
                name = source.name if number == 1 else f'{source.stem} ({number}){suffix}'
                target = self.images / name
                try:
                    output = target.open('xb')
                    break
                except FileExistsError:
                    number += 1
            try:
                with output:
                    shutil.copyfileobj(original, output)
            except OSError:
                target.unlink(missing_ok=True)
                raise
        # Removals are keyed by the managed name, which can be reused after a manual file deletion.
        key = 'imported/' + name
        if key in self.hidden:
            self.set_hidden(key, False)
        return key


class WallpaperDialog(QDialog):
    def __init__(self, parent, backdrop):
        super().__init__(parent)
        self.backdrop = backdrop
        self.setObjectName('lanxiDialog')
        self.setWindowTitle('壁纸管理')
        self.setMinimumSize(720, 465)
        self.resize(765, 500)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(12)
        row = QHBoxLayout()
        row.setSpacing(16)
        layout.addLayout(row, 1)

        self.list = QListWidget()
        self.list.setObjectName('wallpaperList')
        self.list.setAccessibleName('壁纸列表')
        self.list.setFixedWidth(215)
        self.list.setUniformItemSizes(True)
        self.list.currentItemChanged.connect(self.update_selection)
        row.addWidget(self.list)

        details = QVBoxLayout()
        details.setSpacing(7)
        row.addLayout(details, 1)
        self.preview = QLabel()
        self.preview.setObjectName('wallpaperPreview')
        self.preview.setAccessibleName('壁纸原图预览')
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(PREVIEW_SIZE.width() + 10, PREVIEW_SIZE.height() + 10)
        details.addWidget(self.preview, 1)
        self.name = QLabel()
        self.name.setFont(ui_font(13, True))
        self.name.setTextFormat(Qt.TextFormat.PlainText)
        self.name.setWordWrap(True)
        details.addWidget(self.name)
        self.info = QLabel()
        self.info.setObjectName('dialogHint')
        details.addWidget(self.info)
        self.location = QLabel()
        self.location.setObjectName('dialogHint')
        self.location.setTextFormat(Qt.TextFormat.PlainText)
        self.location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.location.setWordWrap(True)
        details.addWidget(self.location)

        self.status = QLabel()
        self.status.setObjectName('dialogHint')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.hide()
        layout.addWidget(self.status)

        actions = QHBoxLayout()
        layout.addLayout(actions)
        self.add = QPushButton('添加图片…')
        self.add.clicked.connect(self.add_images)
        actions.addWidget(self.add)
        self.current = QPushButton('查看当前壁纸')
        self.current.clicked.connect(self.select_current)
        actions.addWidget(self.current)
        actions.addStretch(1)
        self.toggle = QPushButton('移出轮换')
        self.toggle.clicked.connect(self.toggle_selected)
        actions.addWidget(self.toggle)
        self.show_now = QPushButton('立即显示')
        self.show_now.setObjectName('approvalAllow')
        self.show_now.clicked.connect(self.show_selected)
        actions.addWidget(self.show_now)
        close = QPushButton('关闭')
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        for button in (self.add, self.current, self.toggle, self.show_now, close):
            button.setAutoDefault(False)
        self.reload()
        backdrop.wallpaper_changed.connect(self.reload)

    def selected_key(self):
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def reload(self, key=None):
        current = self.backdrop.current_entry
        current_key = current.key if current else None
        selected = key or self.selected_key() or current_key
        self.current.setEnabled(current is not None)
        self.entries = {entry.key: entry for entry in self.backdrop.entries}
        with QSignalBlocker(self.list):
            self.list.clear()
            selected_row = 0
            for row, entry in enumerate(self.entries.values()):
                state = ' · 当前' if entry.key == current_key else (
                    ' · 已移出' if entry.key in self.backdrop.library.hidden else '')
                item = QListWidgetItem(entry.name + state)
                item.setToolTip(entry.name + state)
                item.setData(Qt.ItemDataRole.UserRole, entry.key)
                self.list.addItem(item)
                if entry.key == selected:
                    selected_row = row
            self.list.setCurrentRow(selected_row)
        self.update_selection()

    def update_selection(self):
        entry = self.entries.get(self.selected_key())
        self.toggle.setEnabled(entry is not None)
        self.preview.clear()
        if entry is None:
            self.name.setText('暂无壁纸')
            self.info.clear()
            self.location.clear()
            self.show_now.setEnabled(False)
            return
        hidden = entry.key in self.backdrop.library.hidden
        self.toggle.setText('恢复轮换' if hidden else '移出轮换')
        reader = image_reader(entry.path)
        size = reader.size()
        image = read_preview(reader)
        if image.isNull():
            self.preview.setText('图片暂时无法读取')
        else:
            self.preview.setPixmap(QPixmap.fromImage(image))
        current = self.backdrop.current_entry
        self.show_now.setEnabled(not hidden and not image.isNull() and entry != current)
        self.name.setText(entry.name)
        state = '当前显示' if entry == current else '已移出轮换' if hidden else (
            '内置备用' if entry.origin == 'builtin' and entry not in self.backdrop.wallpapers else '')
        facts = [ORIGINS[entry.origin]]
        if size.isValid():
            facts.append(f'{size.width()} × {size.height()} px')
        if state:
            facts.append(state)
        self.info.setText('  ·  '.join(facts))
        self.location.setText(entry.path.name if entry.origin == 'builtin' else str(entry.path))

    def select_current(self):
        if self.backdrop.current_entry:
            self.reload(self.backdrop.current_entry.key)

    def toggle_selected(self):
        key = self.selected_key()
        hidden = key not in self.backdrop.library.hidden
        try:
            self.backdrop.library.set_hidden(key, hidden)
        except OSError:
            self.set_status('暂时无法保存壁纸设置，请稍后再试。')
            return
        self.backdrop.reload_wallpapers()
        self.reload(key)
        self.set_status('已移出轮换，原图仍保留。' if hidden else '已恢复。')

    def show_selected(self):
        entry = self.entries.get(self.selected_key())
        if entry is not None and not self.backdrop.show_wallpaper(entry):
            self.set_status('暂时无法显示这张壁纸。')
        else:
            self.set_status('')

    def set_status(self, text):
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def add_images(self):
        names, _ = QFileDialog.getOpenFileNames(self, '添加壁纸', '', '图片 (*.jpg *.jpeg *.png)')
        added, failed = [], []
        for name in names:
            path = Path(name)
            try:
                added.append(self.backdrop.library.import_image(path))
            except (OSError, ValueError):
                failed.append(path.name)
        if added:
            self.backdrop.reload_wallpapers()
            self.reload(added[0])
        if failed:
            self.set_status(f'已添加 {len(added)} 张，{len(failed)} 张无法读取或保存。首张失败图片为 {failed[0]}。')
        elif added:
            self.set_status(f'已添加 {len(added)} 张壁纸。')
