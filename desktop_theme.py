"""Blue desktop workspace tokens, native fonts and readable conversation HTML."""
import re
from functools import lru_cache
from html import escape

from PySide6.QtGui import QFont, QFontDatabase, QTextBlockFormat, QTextCursor, QTextOption

PALETTE = {
    'paper': '#f3f6fb', 'surface': '#ffffff', 'ink': '#24344d',
    'heading': '#1c3151', 'muted': '#61738c', 'line': '#e1e8f2',
    'primary': '#2b63c6', 'primary_hover': '#2458b4', 'primary_pressed': '#1e4b9c',
    'soft': '#edf3ff', 'soft_ink': '#315d9b', 'soft_hover': '#e3edff',
    'soft_pressed': '#d8e6fc', 'disabled': '#f1f3f7', 'disabled_ink': '#8996a8',
    'warning': '#825719', 'warning_surface': '#fff7e9',
}
# Prefer a native family with explicit Regular/Bold faces. Some locally installed
# Noto variants resolve to a light face when Qt applies stylesheet font weights.
FONT_CANDIDATES = ('Microsoft YaHei UI', 'Microsoft YaHei', 'Noto Sans SC', 'DengXian')
LATIN_FONTS = ('Segoe UI', 'Arial')


@lru_cache(maxsize=1)
def font_families():
    available = set(QFontDatabase.families())
    chinese = next((name for name in FONT_CANDIDATES if name in available), 'Sans Serif')
    latin = next((name for name in LATIN_FONTS if name in available), 'Arial')
    return chinese, latin


def ui_font(size=10, strong=False):
    chinese, latin = font_families()
    font = QFontDatabase.font(chinese, 'Bold' if strong else 'Regular', round(size))
    font.setFamilies([chinese, latin])
    font.setPointSizeF(float(size))
    font.setWeight(QFont.Weight.Bold if strong else QFont.Weight.Normal)
    font.setStyleName('Bold' if strong else 'Regular')
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    return font


def configure_text(widget, size=10, inset=10):
    font = ui_font(size)
    widget.setFont(font)
    widget.document().setDefaultFont(font)
    widget.setCurrentFont(font)
    widget.document().setDocumentMargin(inset)
    widget.document().setDefaultStyleSheet('p { margin-top:3px; margin-bottom:8px; }')
    widget.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    block = QTextBlockFormat()
    block.setLineHeight(150, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
    widget.textCursor().mergeBlockFormat(block)


def set_reading_html(widget, html):
    widget.setHtml(html)
    block = widget.document().begin()
    while block.isValid():
        cursor = QTextCursor(block)
        fmt = block.blockFormat()
        table = cursor.currentTable()
        if table is not None:
            cell_background = table.cellAt(cursor).format().background()
            # HTML import also assigns the cell color to its text runs.
            if fmt.background() == cell_background:
                fmt.clearBackground()
            runs = []
            fragment = block.begin()
            while not fragment.atEnd():
                item = fragment.fragment()
                char_format = item.charFormat()
                if char_format.hasProperty(char_format.Property.BackgroundBrush) and char_format.background() == cell_background:
                    char_format.clearBackground()
                    runs.append((item.position(), item.length(), char_format))
                fragment += 1
            for position, length, char_format in runs:
                selection = QTextCursor(widget.document())
                selection.setPosition(position)
                selection.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
                selection.setCharFormat(char_format)
        fmt.setLineHeight(150, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        cursor.setBlockFormat(fmt)
        block = block.next()


def _inline(text):
    content = escape(text)
    content = re.sub(r'`([^`\n]+)`', r'<span style="background:#edf2fa;color:#284e82;font-family:Consolas;">\1</span>', content)
    content = re.sub(r'\*\*([^*\n]+)\*\*', r'<b>\1</b>', content)
    return content.replace('\n', '<br>')


def message_html(role, text, assistant):
    blocks = []
    for index, part in enumerate(re.split(r'```[^\n]*\n([\s\S]*?)```', str(text or ''))):
        if not part:
            continue
        if index % 2:
            content = escape(part.rstrip('\n')).replace('\n', '<br>').replace(' ', '&nbsp;')
            blocks.append('<table width="100%" cellspacing="0" cellpadding="10" bgcolor="#f0f4fa">'
                          '<tr><td style="color:#294667;font-family:Consolas;font-size:9pt;">' + content + '</td></tr></table>')
        else:
            blocks.extend('<p style="color:#24344d;font-size:10pt;">' + _inline(paragraph) + '</p>'
                          for paragraph in part.strip().split('\n\n') if paragraph)
    surface = 'rgba(235,249,255,0.62)' if assistant else 'rgba(214,240,255,0.84)'
    spacer = '<td width="7%"></td>'
    bubble = (f'<td width="93%" style="padding:10px;background-color:{surface};">'
              '<p style="margin-top:0;margin-bottom:7px;font-size:9pt;color:#61738c;">'
              f'<b>{escape(role)}</b></p>{"".join(blocks)}</td>')
    return ('<table width="100%" cellspacing="0" cellpadding="0"><tr>' +
            (bubble + spacer if assistant else spacer + bubble) +
            '</tr></table><p style="margin:0;font-size:5pt;">&nbsp;</p>')


def warning_html(text):
    return ('<table width="100%" cellspacing="0" cellpadding="12" bgcolor="#fff7e9">'
            '<tr><td><p style="font-size:9pt;color:#825719;"><b>! 执行未完成</b></p>'
            '<p style="font-size:10pt;color:#665437;">' + escape(str(text)).replace('\n', '<br>') +
            '</p></td></tr></table>')


STYLESHEET = '''
QMainWindow { background:#25465e; color:#24344d; }
QFrame#statDivider { background:rgba(183, 222, 245, 59); }
QFrame#composerBox { background:rgba(225, 247, 255, 80); border:1px solid rgba(226, 248, 255, 160); border-radius:11px; }
QFrame#divider { background:rgba(230, 249, 255, 118); border:0; max-height:1px; }
QLabel { color:#24344d; background:transparent; }
QLabel#status[connected="true"] { color:#154e41; }
QLineEdit { background:rgba(234, 249, 255, 188); color:#15364d; border:1px solid rgba(234, 250, 255, 182); border-radius:8px; padding:5px 9px; min-height:22px; }
QLineEdit:focus { border-color:#327fb2; }
QListWidget#taskList { background:transparent; border:0; color:#16384d; outline:0; }
QListWidget#taskList::item { border:1px solid transparent; border-radius:9px; padding:7px 9px; }
QListWidget#taskList::item:hover { background:rgba(226, 246, 255, 100); }
QListWidget#taskList::item:selected { background:rgba(212, 238, 255, 165); border-color:rgba(219, 247, 255, 150); color:#113951; }
QListWidget#taskList::item:focus { border-color:#327fb2; }
QTextEdit, QTextBrowser { color:#24344d; background:transparent; border:0; padding:0; selection-background-color:#d9e6fc; selection-color:#1c3151; }
QTextEdit#composer { background:transparent; border:0; }
QTextBrowser#conversation, QTextBrowser#infoText { background:transparent; border:0; }
QPushButton { background:#ffffff; color:#425a7b; border:1px solid #dae3f0; border-radius:8px; padding:7px 11px; }
QPushButton:hover { background:#f2f6fe; border-color:#a9c1e7; color:#28599f; }
QPushButton:pressed { background:#e3edff; border-color:#83a7dd; }
QPushButton:focus { border-color:#7399db; }
QPushButton#clientSwitch { background:transparent; color:#f1faff; border:1px solid transparent; border-radius:16px; padding:0; }
QPushButton#clientSwitch:hover { background:rgba(180, 231, 251, 21); }
QPushButton#clientSwitch:pressed { background:rgba(143, 214, 243, 75); }
QPushButton#clientSwitch:focus { border-color:#93dffa; }
QPushButton#hoverAction { background:rgba(225, 247, 255, 154); color:#153b53; border-color:rgba(226, 245, 255, 138); padding:5px 8px; }
QPushButton#hoverAction:hover, QPushButton#hoverAction:focus { background:#d4efff; border-color:#70a8cc; }
QPushButton#hoverAction:pressed { background:#acd8f1; }
QPushButton#send, QPushButton#approvalAllow { background:#096cb9; color:white; border-color:#096cb9; }
QPushButton#send { padding:0; border-radius:17px; font-size:18px; }
QPushButton#send:hover, QPushButton#approvalAllow:hover { background:#055eaa; border-color:#055eaa; }
QPushButton#send:pressed, QPushButton#approvalAllow:pressed { background:#044e90; }
QPushButton:disabled, QPushButton#send:disabled, QPushButton#hoverAction:disabled, QPushButton#approvalAllow:disabled { background:rgba(234, 246, 252, 97); color:#507087; border-color:rgba(225, 245, 255, 80); }
QDialog#approvalDialog { background:#d6eaf5; }
QTextBrowser#approvalDetails { background:#e6f2fb; border:1px solid #bfd9ea; border-radius:8px; }
QScrollArea { border:0; background:transparent; }
QScrollBar:vertical { background:transparent; width:7px; margin:3px 0; }
QScrollBar::handle:vertical { background:#d0dae9; border-radius:3px; min-height:26px; }
QScrollBar::handle:vertical:hover { background:#afc0d8; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:transparent; }
QMenu { background:#ffffff; color:#304664; border:1px solid #dce5f2; border-radius:8px; padding:5px; }
QMenu::item { padding:8px 20px; border-radius:5px; }
QMenu::item:selected { background:#edf3ff; color:#275db5; }
QToolTip { background:#1c3151; color:#ffffff; border:0; padding:7px; }
'''
