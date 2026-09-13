"""Locate package data whether we are a checkout or a frozen binary."""

import sys
from pathlib import Path


def package_dir() -> Path:
    """Directory that holds ``bootstrap.py`` and ``web/``.

    This module is bytecode inside the PYZ in a freeze, so ``Path(__file__)``
    is not a real file next to those two. PyInstaller puts data under
    ``sys._MEIPASS`` — ``_internal/`` beside the onedir executable.
    """
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        bundled = meipass / "odoo_sheller"
        if (bundled / "bootstrap.py").is_file():

            return bundled

        return meipass

    return Path(__file__).resolve().parent


def web_dir() -> Path:

    return package_dir() / "web"


def bootstrap_path() -> Path:

    return package_dir() / "bootstrap.py"
