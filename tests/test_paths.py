"""Package data must resolve the same way in a checkout and in a freeze."""

import sys
from pathlib import Path

from odoo_sheller.paths import bootstrap_path, package_dir, web_dir


def test_package_dir_is_the_source_tree_when_not_frozen():
    root = package_dir()
    assert root.name == "odoo_sheller"
    assert (root / "bootstrap.py").is_file()
    assert (root / "web" / "index.html").is_file()
    assert web_dir() == root / "web"
    assert bootstrap_path() == root / "bootstrap.py"


def test_package_dir_uses_meipass_when_frozen(monkeypatch, tmp_path):
    bundled = tmp_path / "odoo_sheller"
    bundled.mkdir()
    (bundled / "bootstrap.py").write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert package_dir() == bundled
    assert bootstrap_path() == bundled / "bootstrap.py"


def test_package_dir_falls_back_to_meipass_root_without_a_package_subdir(
    monkeypatch, tmp_path
):
    (tmp_path / "bootstrap.py").write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert package_dir() == tmp_path


def test_api_and_transport_read_the_same_on_disk_files():
    from odoo_sheller.api import WEB
    from odoo_sheller.transport import bootstrap_source

    assert WEB == web_dir()
    assert (WEB / "vendor" / "codemirror.min.js").is_file()
    source = bootstrap_source()
    assert "_os_main(globals())" in source
    assert "OS_CMD_FD" in source


def test_meipass_is_not_confused_with_the_checkout(monkeypatch, tmp_path):
    """A freeze that forgot --add-data must not silently pick up a checkout."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    checkout = Path(__file__).resolve().parent.parent / "odoo_sheller"

    assert package_dir() == tmp_path
    assert package_dir() != checkout
    assert not bootstrap_path().is_file()
