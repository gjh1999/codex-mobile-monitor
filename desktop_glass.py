"""Native wallpaper composition and the desktop client switch."""
import sys
from math import exp
from pathlib import Path
from time import monotonic

from PySide6.QtCore import QEvent, QEasingCurve, QPoint, QPointF, QRectF, QStandardPaths, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QImage, QImageReader, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QPushButton, QWidget

WALLPAPERS = ('os-x-mavericks-wave.jpg', 'macos-big-sur-day.jpg', 'macos-catalina-night.jpg')


def local_wallpapers():
    pictures = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
    if not pictures:
        return []
    library = Path(pictures) / '澜汐' / 'macOS'
    images = []
    for path in library.rglob('*'):
        if path.suffix.lower() in {'.jpg', '.jpeg', '.png'} and path.is_file():
            size = QImageReader(str(path)).size()
            if size.width() >= size.height() > 0:
                images.append(path)
    return sorted(images, key=lambda path: (path.stem != '10.9 Mavericks', path))


def motion_enabled():
    if sys.platform == 'win32':
        import ctypes
        enabled = ctypes.c_int(1)
        if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0):
            return bool(enabled.value)
    return True


class OceanBackdrop(QWidget):
    def __init__(self, bundle: Path, minutes):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        assets = bundle / 'docs' / 'assets'
        bundled = [assets / name for name in WALLPAPERS if (assets / name).is_file()]
        if type(minutes) is not int or not 0 <= minutes <= 1440:
            minutes = 30
        self.wallpapers = local_wallpapers()
        if minutes == 0:
            self.wallpapers = [path for path in self.wallpapers if path.stem == '10.9 Mavericks']
        self.index = 0
        self.current = self.load_wallpaper()
        if self.current[0].isNull():
            self.wallpapers = bundled
            self.current = self.load_wallpaper()
        self.previous = None
        self.fade = 1.0
        self.transition = QVariantAnimation(self)
        self.transition.setStartValue(0.0)
        self.transition.setEndValue(1.0)
        self.transition.setDuration(600)
        self.transition.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.transition.valueChanged.connect(self.set_fade)
        self.transition.finished.connect(self.finish_fade)
        self.rotation = QTimer(self)
        self.rotation.timeout.connect(self.next_wallpaper)
        if minutes and len(self.wallpapers) > 1:
            self.rotation.start(minutes * 60_000)

    def load_wallpaper(self):
        while self.wallpapers:
            self.index %= len(self.wallpapers)
            image = QImage(str(self.wallpapers[self.index]))
            if not image.isNull():
                soft = image.scaledToWidth(100, Qt.TransformationMode.SmoothTransformation)
                return image, soft
            self.wallpapers.pop(self.index)
        self.index = 0
        return QImage(), QImage()

    def next_wallpaper(self):
        if len(self.wallpapers) < 2:
            return
        self.index = (self.index + 1) % len(self.wallpapers)
        next_image = self.load_wallpaper()
        if next_image[0].isNull():
            return
        self.transition.stop()
        self.previous, self.current = self.current, next_image
        if not self.isVisible() or not motion_enabled():
            self.finish_fade()
        else:
            self.set_fade(0.0)
            self.transition.start()

    def set_fade(self, value):
        self.fade = value
        self.update()
        for panel in self.findChildren(FrostedPanel):
            panel.update()

    def finish_fade(self):
        self.previous = None
        self.set_fade(1.0)

    def draw_wallpaper(self, painter, blurred=False):
        bounds = QRectF(self.rect())
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        layer = int(blurred)
        if self.previous:
            self.draw_image(painter, bounds, self.previous[layer])
        painter.setOpacity(self.fade)
        self.draw_image(painter, bounds, self.current[layer])
        painter.setOpacity(1.0)

    @staticmethod
    def draw_image(painter, bounds, image):
        if image.isNull() or bounds.isEmpty():
            return
        scale = max(bounds.width() / image.width(), bounds.height() / image.height())
        width, height = bounds.width() / scale, bounds.height() / scale
        source = QRectF((image.width() - width) / 2, (image.height() - height) / 2, width, height)
        painter.drawImage(bounds, image, source)

    def paintEvent(self, event):
        painter = QPainter(self)
        bounds = QRectF(self.rect())
        painter.fillRect(bounds, QColor('#25465e'))
        self.draw_wallpaper(painter)
        shade = QLinearGradient(0, 0, 0, 236)
        shade.setColorAt(0, QColor(7, 26, 44, 56))
        shade.setColorAt(.52, QColor(7, 26, 44, 104))
        shade.setColorAt(1, QColor(7, 26, 44, 0))
        painter.fillRect(bounds, shade)


class FrostedPanel(QFrame):
    def __init__(self, backdrop, *, radius=16, tint=None, edge=None):
        super().__init__()
        self.backdrop = backdrop
        self.radius = radius
        self.tint = tint or QColor(202, 236, 250, 151)
        self.edge = edge or QColor(231, 248, 255, 164)

    def paint_glass(self, painter, bounds, radius, tint, edge):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        glass = QPainterPath()
        glass.addRoundedRect(bounds.adjusted(.5, .5, -.5, -.5), radius, radius)
        painter.save()
        painter.setClipPath(glass)
        painter.translate(-self.mapTo(self.backdrop, QPoint()))
        self.backdrop.draw_wallpaper(painter, blurred=True)
        painter.restore()
        painter.fillPath(glass, tint)
        if edge is not None:
            painter.setPen(QPen(edge))
            painter.drawPath(glass)

    def paintEvent(self, event):
        painter = QPainter(self)
        self.paint_glass(painter, QRectF(self.rect()), self.radius, self.tint, self.edge)


class ClientSegment(QPushButton):
    """Keep both labels readable as the glass thumb passes under them."""
    def paintEvent(self, event):
        switch = self.parentWidget()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(self.font())
        bounds = QRectF(self.rect())
        if not switch.dragging and (self.isDown() or self.underMouse()):
            feedback = QPainterPath()
            feedback.addRoundedRect(bounds, bounds.height() / 2, bounds.height() / 2)
            painter.fillPath(feedback, QColor(194, 237, 252, 42 if self.isDown() else 15))

        thumb = switch.thumb_rect().translated(-QPointF(self.pos()))
        thumb_clip = QPainterPath()
        thumb_clip.addRoundedRect(thumb, thumb.height() / 2, thumb.height() / 2)
        outside = QPainterPath()
        outside.addRect(bounds)
        painter.save()
        painter.setClipPath(outside.subtracted(thumb_clip))
        painter.setPen(QColor('#f3fcff'))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())
        painter.restore()
        painter.save()
        painter.setClipPath(thumb_clip)
        painter.setPen(QColor('#0a2a40'))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())
        painter.restore()
        if self.hasFocus() and not self.isDown() and not switch.dragging:
            painter.setPen(QPen(QColor('#d1f0fc'), 1))
            ring = bounds.adjusted(.5, .5, -.5, -.5)
            painter.drawRoundedRect(ring, ring.height() / 2, ring.height() / 2)


class ClientSwitch(FrostedPanel):
    # The angular frequency sets how quickly the critically damped thumb settles.
    SPRING_RATE = 25.0

    def __init__(self, backdrop, select):
        super().__init__(backdrop, radius=21, tint=QColor(8, 54, 70, 141), edge=QColor(210, 245, 255, 77))
        self.setObjectName('clientPill')
        self.setAccessibleName('切换工作区')
        self.select_workspace = select
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 6, 14, 6)
        layout.setSpacing(5)
        self.buttons = {}
        self.offsets = {}
        offset = 0
        for key, text, width in (('codex', 'Codex', 84), ('claude', 'Claude Code', 104), ('notebook', '吉光片羽', 99)):
            button = ClientSegment(text)
            font = button.font()
            font.setPointSizeF(11.0)
            font.setWeight(font.Weight.Bold)
            font.setStyleName('Bold')
            button.setFont(font)
            button.setFixedSize(width, 30)
            button.setObjectName('clientSwitch')
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setCheckable(True)
            button.installEventFilter(self)
            button.clicked.connect(lambda _=False, section=key: self.select_workspace(section))
            layout.addWidget(button)
            self.buttons[key] = button
            self.offsets[key] = offset
            offset += width + layout.spacing()
        self.active = 'codex'
        self.position = self.velocity = 0.0
        self.target = 0.0
        self.last_frame = 0.0
        self.press = None
        self.dragging = False
        self.spring = QTimer(self)
        self.spring.setTimerType(Qt.TimerType.PreciseTimer)
        self.spring.setInterval(16)
        self.spring.timeout.connect(self.advance_spring)
        self.select('codex', animate=False)

    def thumb_rect(self):
        left_key, right_key = ('codex', 'claude') if self.position <= self.offsets['claude'] else ('claude', 'notebook')
        left, right = self.buttons[left_key], self.buttons[right_key]
        interval = self.offsets[right_key] - self.offsets[left_key]
        progress = max(0.0, min(1.0, (self.position - self.offsets[left_key]) / interval))
        first = self.buttons['codex']
        return QRectF(first.x() + self.position, first.y(),
                      left.width() + (right.width() - left.width()) * progress, first.height())

    def repaint_switch(self):
        self.update()
        for button in self.buttons.values():
            button.update()

    def select(self, client, animate=True):
        self.active = client
        for key, button in self.buttons.items():
            button.setChecked(key == client)
        self.target = float(self.offsets[client])
        if not animate or not self.isVisible() or not motion_enabled():
            self.spring.stop()
            self.position, self.velocity = self.target, 0.0
            self.repaint_switch()
        elif not self.spring.isActive() and (self.position != self.target or self.velocity):
            self.last_frame = monotonic()
            self.spring.start()

    def advance_spring(self):
        now = monotonic()
        elapsed = now - self.last_frame
        self.last_frame = now
        rate = self.SPRING_RATE
        distance = self.position - self.target
        carry = self.velocity + rate * distance
        decay = exp(-rate * elapsed)
        self.position = self.target + (distance + carry * elapsed) * decay
        self.velocity = (self.velocity - rate * carry * elapsed) * decay
        if abs(self.position - self.target) < .1 and abs(self.velocity) < 1.35:
            self.spring.stop()
            self.position, self.velocity = self.target, 0.0
        self.repaint_switch()

    def eventFilter(self, button, event):
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            point = event.position() + QPointF(button.pos())
            if self.thumb_rect().contains(point):
                self.spring.stop()
                self.velocity = 0.0
                self.press = (point.x(), self.position, monotonic())
                self.dragging = False
        elif kind == QEvent.Type.MouseMove and self.press is not None:
            x = event.position().x() + button.x()
            anchor, start, last = self.press
            if self.dragging or abs(x - anchor) >= QApplication.startDragDistance():
                self.dragging = True
                now = monotonic()
                position = max(0.0, min(self.offsets['notebook'], start + x - anchor))
                self.velocity = (position - self.position) / max(now - last, .01)
                self.position = position
                self.press = (anchor, start, now)
                self.repaint_switch()
                return True
        elif (kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton
              and self.press is not None):
            _, _, last = self.press
            self.press = None
            if self.dragging:
                self.dragging = False
                button.setDown(False)
                # Limit momentum so a quick release travels to the nearby stop.
                velocity = self.velocity if monotonic() - last < .08 else 0.0
                self.velocity = max(-480.0, min(480.0, velocity))
                projected = self.position + self.velocity * .055
                section = min(self.offsets, key=lambda key: abs(self.offsets[key] - projected))
                self.select_workspace(section)
                return True
            self.select(self.active)
        elif kind == QEvent.Type.UngrabMouse and self.press is not None:
            self.press = None
            self.dragging = False
            button.setDown(False)
            self.select(self.active)
        return super().eventFilter(button, event)

    def paintEvent(self, event):
        painter = QPainter(self)
        self.paint_glass(painter, QRectF(self.rect()), self.radius, self.tint, self.edge)
        thumb = self.thumb_rect()
        self.paint_glass(painter, thumb, thumb.height() / 2, QColor(215, 243, 253, 146), None)
