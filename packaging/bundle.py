"""Shared PyInstaller setup for the daemon and the MCP server.

The spec files import this. Keep the --add-data list and copy_metadata here
so a path change is reviewed in one place rather than in someone's shell
history.
"""

from pathlib import Path

from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.building.build_main import Analysis
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(__file__).resolve().parent.parent


def _datas(kind: str) -> list:
    datas = copy_metadata("odoo-sheller")
    if kind == "daemon":
        datas += collect_data_files("odoo_sheller")
        # bootstrap.py is executed as source inside the container, not imported
        # here, so collect_data_files skips it. Without this the app launches
        # and fails on the first session.
        datas.append((str(ROOT / "odoo_sheller" / "bootstrap.py"), "odoo_sheller"))

    return datas


def _hidden(kind: str) -> list[str]:
    if kind == "daemon":
        # uvicorn picks protocol and lifespan implementations by string name.

        return collect_submodules("uvicorn")

    return collect_submodules("mcp") + ["mcp_types", "httpx2"]


def analysis(kind: str, script: str) -> Analysis:

    return Analysis(
        [str(ROOT / "packaging" / script)],
        pathex=[str(ROOT)],
        binaries=[],
        datas=_datas(kind),
        hiddenimports=_hidden(kind),
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=[],
        noarchive=False,
    )


def onedir(analysis_obj: Analysis, name: str) -> COLLECT:
    pyz = PYZ(analysis_obj.pure)
    exe = EXE(
        pyz,
        analysis_obj.scripts,
        [],
        exclude_binaries=True,
        name=name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )

    return COLLECT(
        exe,
        analysis_obj.binaries,
        analysis_obj.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=name,
    )
