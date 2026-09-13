#!/bin/sh
# Freeze the daemon and the MCP server into onedir trees under packaging/dist/.
# Tauri copies those trees into Contents/Resources/ at bundle time.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ "${SKIP_FREEZE:-}" = "1" ] \
    && [ -x packaging/dist/odoo-sheller/odoo-sheller ] \
    && [ -x packaging/dist/odoo-sheller-mcp/odoo-sheller-mcp ]; then
    exit 0
fi

uv sync --group dev --frozen
uv run pyinstaller --noconfirm \
    --distpath packaging/dist \
    --workpath packaging/build/odoo-sheller \
    packaging/odoo-sheller.spec
uv run pyinstaller --noconfirm \
    --distpath packaging/dist \
    --workpath packaging/build/odoo-sheller-mcp \
    packaging/odoo-sheller-mcp.spec

# Placeholders so `cargo test` still compiles after a freeze that replaced the
# trees. The binaries themselves stay gitignored.
touch packaging/dist/odoo-sheller/.gitkeep
touch packaging/dist/odoo-sheller-mcp/.gitkeep
