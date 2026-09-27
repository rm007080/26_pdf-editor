# -*- mode: python ; coding: utf-8 -*-
# フォルダ形式。1 ファイル形式にはしない。
# ログイン時の自動起動は登録しない。Run キーにもショートカットを作らない。
# ビルドの前に、web で npm run build を行う。
#   cd web
#   npm run build
#   cd ..
#   pyinstaller pdf-editor.spec
# この計画では exe 自体は組み立てない。
# console=False は、トレイ以外にコンソール窓を出さないため。

a = Analysis(
    ['app/__main__.py'],
    pathex=[],
    binaries=[],
    datas=[('web/dist', 'web/dist')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='pdf-editor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='pdf-editor',
)
