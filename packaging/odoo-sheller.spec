# onedir freeze of the daemon. Invoked by packaging/freeze.sh.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(SPECPATH).resolve()))
from bundle import analysis, onedir

coll = onedir(analysis("daemon", "run_daemon.py"), "odoo-sheller")
