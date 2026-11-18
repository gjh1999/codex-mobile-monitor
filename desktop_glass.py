"""Native wallpaper composition and the desktop client switch."""
import sys
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QPoint, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton, QWidget

WALLPAPERS = ('os-x-mavericks-wave.jpg', 'macos-big-sur-day.jpg', 'macos-catalina-night.jpg')


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
        self.assets = bundle / 'docs' / 'assets'
        self.wallpapers = [name for name in WALLPAPERS if (self.assets / name).is_file()]
        self.index = 0
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
        if type(minutes) is not int or not 0 <= minutes <= 1440:
            minutes = 30
        if minutes and len(self.wallpapers) > 1:
            self.rotation.start(minutes * 60_000)

    def load_wallpaper(self):
        if not self.wallpapers:
            return QImage(), QImage()
        image = QImage(str(self.assets / self.wallpapers[self.index]))
        # Downsample once per wallpaper, then interpolate in the painter.
        soft = image.scaledToWidth(100, Qt.TransformationMode.SmoothTransformation)
        return image, soft

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
    def __init__(self, backdrop):
        super().__init__()
        self.backdrop = backdrop

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        border = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        glass = QPainterPath()
        glass.addRoundedRect(border, 16, 16)
        painter.save()
        painter.setClipPath(glass)
        painter.translate(-self.mapTo(self.backdrop, QPoint()))
        self.backdrop.draw_wallpaper(painter, blurred=True)
        painter.restore()
        painter.fillPath(glass, QColor(202, 236, 250, 151))
        painter.setPen(QPen(QColor(231, 248, 255, 164)))
        painter.drawPath(glass)


class ClientSwitch(QFrame):
    def __init__(self, select):
        super().__init__()
        self.setObjectName('clientPill')
        self.setAccessibleName('客户端')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(1)
        self.thumb = QFrame(self)
        self.thumb.setObjectName('clientThumb')
        self.thumb.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.buttons = {}
        for key, text in (('codex', 'Codex'), ('claude', 'Claude Code')):
            button = QPushButton(text)
            button.setFixedSize(111, 32)
            button.setObjectName('clientSwitch')
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setCheckable(True)
            button.clicked.connect(lambda _=False, client=key: select(client))
            layout.addWidget(button)
            self.buttons[key] = button
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(240)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.valueChanged.connect(self.thumb.move)
        self.active = 'codex'
        self.select('codex', animate=False)

    def select(self, client, animate=True):
        self.active = client
        for key, button in self.buttons.items():
            button.setChecked(key == client)
        self.animation.stop()
        button = self.buttons[client]
        self.thumb.resize(button.size())
        if animate and self.isVisible() and motion_enabled():
            self.animation.setStartValue(self.thumb.pos())
            self.animation.setEndValue(button.pos())
            self.animation.start()
        else:
            self.thumb.move(button.pos())

    def showEvent(self, event):
        self.select(self.active, animate=False)
        super().showEvent(event)
