# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['sclean.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('sclean.ico', '.'),
        ('sclean_logo.png', '.'),
        ('sclean_logo_small.png', '.'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # pystray/Pillow больше не используются (см. sclean.py) — trey
    # реализован через обычное сворачивание окна, без сторонних
    # зависимостей. Явно исключаем их и tkinter.test/unittest на случай,
    # если что-то потянет их транзитивно — уменьшает вес exe.
    excludes=['PIL', 'pystray', 'unittest', 'test', 'tkinter.test'],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='sclean',
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
    icon='sclean.ico',
    uac_admin=True,
)
