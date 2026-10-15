# -*- mode: python ; coding: utf-8 -*-
# Build:  pyinstaller packaging/DiscordPro.spec --noconfirm
# Output: dist/DiscordPro/ (one-folder build; faster start and fewer AV false positives than one-file)

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 (provided by PyInstaller)

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("websockets")
    + collect_submodules("discord_pro")
    + ["pypresence", "psutil"]
)

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "discord_pro" / "web"), "discord_pro/web")],
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "playwright"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DiscordPro",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="DiscordPro", upx=False)  # noqa: F821
