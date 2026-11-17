# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:/Users/gjh/PycharmProjects/To_Do_List/codex-mobile-monitor/desktop_app.py'],
    pathex=['C:/Users/gjh/PycharmProjects/To_Do_List/codex-mobile-monitor'],
    binaries=[],
    datas=[('C:/Users/gjh/PycharmProjects/To_Do_List/codex-mobile-monitor/windows_notify.ps1', '.'), ('C:/Users/gjh/PycharmProjects/To_Do_List/codex-mobile-monitor/app.ico', '.')],
    hiddenimports=['dingtalk_commands', 'desktop_win32', 'desktop_qt'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', '_tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='CodexClaudeMonitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['C:/Users/gjh/PycharmProjects/To_Do_List/codex-mobile-monitor/app.ico'],
)
