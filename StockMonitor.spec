# -*- mode: python ; coding: utf-8 -*-
# 唯一的打包定义：本地与 GitHub Actions 都用 `pyinstaller --noconfirm --clean StockMonitor.spec`，
# 避免「命令行参数一份、spec 一份」两套口径各自漂移。
# 注意：CI 在打包前会用 watchlist_config.example.json 覆盖 scripts/watchlist_config.json，
# 否则会把开发者本机的自选股（和加密凭证）打进对外发布的产物里。

a = Analysis(
    ['floating_window.py'],
    pathex=['scripts'],
    binaries=[],
    datas=[('scripts/watchlist_config.json', 'scripts')],
    hiddenimports=['price_alert', 'alpaca_history', 'alpaca_stream', 'secret_store', 'tzdata'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineQuick',
        'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuick3D',
        'PySide6.Qt3DCore', 'PySide6.Qt3DRender',
        'PySide6.QtMultimedia', 'PySide6.QtCharts', 'PySide6.QtDataVisualization',
        'tkinter',
    ],
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
    name='StockMonitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
