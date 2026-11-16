"""The Settings sheet colours its snippets with a tokenizer of its own.

It runs in the app's webview, so it is checked here through node: the tokens
must put the text back together exactly — a highlighter that drops or doubles a
character would show one thing and copy another — and each language must mark
what a reader looks for in it.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).parent.parent / "odoo-sheller-app" / "src" / "highlight.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def tokens(text, lang):
    script = (
        f"const {{ highlight }} = require({json.dumps(str(SOURCE))});"
        f"process.stdout.write(JSON.stringify(highlight({json.dumps(text)}, {json.dumps(lang)})));"
    )
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return [tuple(pair) for pair in json.loads(done.stdout)]


def kinds(text, lang):
    return {(kind, part) for kind, part in tokens(text, lang) if kind}


JSON_TEXT = """{
  "mcpServers": {
    "odoo-sheller": {
      "args": ["--directory", "/my \\"repo\\""],
      "command": "/Users/x/.odoo-sheller/bin/odoo-sheller-mcp"
    }
  }
}"""

TOML_TEXT = """[mcp_servers.odoo-sheller]
command = "/opt/homebrew/bin/uv"
args = ["--directory", "/my \\"repo\\""]"""

SSH_TEXT = "Host *.odoo.com\n    StrictHostKeyChecking accept-new\n"


@pytest.mark.parametrize(
    ("text", "lang"),
    [(JSON_TEXT, "json"), (TOML_TEXT, "toml"), (SSH_TEXT, "ssh"), ("plain", None), ("x", "cobol")],
)
def test_the_tokens_are_the_text(text, lang):
    assert "".join(part for _, part in tokens(text, lang)) == text


def test_json_tells_keys_from_strings():
    found = kinds(JSON_TEXT, "json")
    assert ("key", '"mcpServers"') in found
    assert ("key", '"command"') in found
    assert ("string", '"/Users/x/.odoo-sheller/bin/odoo-sheller-mcp"') in found
    assert ("string", '"/my \\"repo\\""') in found, "an escaped quote does not end a string"
    assert ("punct", "{") in found


def test_toml_marks_the_table_keys_and_strings():
    found = kinds(TOML_TEXT, "toml")
    assert ("section", "[mcp_servers.odoo-sheller]") in found
    assert ("key", "command") in found
    assert ("key", "args") in found
    assert ("string", '"/opt/homebrew/bin/uv"') in found
    assert ("string", '"/my \\"repo\\""') in found


def test_ssh_config_marks_keywords_and_values():
    found = kinds(SSH_TEXT, "ssh")
    assert ("key", "Host") in found
    assert ("key", "StrictHostKeyChecking") in found
    assert ("string", "*.odoo.com") in found
    assert ("string", "accept-new") in found


def test_an_unknown_language_is_left_plain():
    assert tokens("a = 1", "cobol") == [("", "a = 1")]
    assert tokens("a = 1", None) == [("", "a = 1")]
