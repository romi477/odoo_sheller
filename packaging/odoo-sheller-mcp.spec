# onedir freeze of the MCP server. Invoked by packaging/freeze.sh.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(SPECPATH).resolve()))
from bundle import analysis, onedir

coll = onedir(analysis("mcp", "run_mcp.py"), "odoo-sheller-mcp")
