"""The guidance the daemon carries: what an MCP client is told, and its HTTP form.

The same texts reach two kinds of client. An MCP agent reads them through
os_help and the server's instructions; an HTTP client reads them from
/api/help and names the tools itself. The record below is what the MCP side
said before the texts learned to leave tool names to the caller, and it is
what keeps that change invisible to every MCP client.
"""

import hashlib
import re

from odoo_sheller import guidance, rules
from odoo_sheller import mcp as server

# SHA-256 of every text as an MCP client receives it. Change an entry only in
# a commit that changes that text's wording on purpose, never to make a
# refactor pass.
MCP_GOLDEN = {
    "instructions": "beacbc366d5f084e6c2576d58e865c2d2f924912ac81862e02141975a3d56df3",
    "sessions": "f279071f06d2a72b141caac228164e467205feedd8a355a352d5ca1dc2156bb5",
    "ownership": "a7dfdbc2da4f62f93ac40110271f76e3e3607b2b53ded4432ae37609bde313c6",
    "commit": "1a3ce3b23e6b0113939ce8713ab3d3acdd797fbc1ae013047ff394741b460155",
    "remote_server": "809bb8517bf1789aecb3d8ae60a5bcd3234b98e6441cce3d1a64d577ed3a316e",
    "limits": "20dd27192499f9f42ce507560287d6b25dedb174d5076bbb0b247df65b4e040b",
    "orm": "29ce479be88e26e3c260c056cc2e71c2001be5069a1de0e8c6ef308a4aefd031",
    "code": "9695582a4342042586018028a87e617a9adffe8333083f67a3b4326914a14638",
    "log": "25be6d6b541c96978a501be05720c193444cdda9b29671724676c2673184b1a3",
    "records": "5b2c65b96dd3ba9f796619631e74644d1be58469e92cb2db1c8085c56db054a5",
    "modules": "d9f21e78ff3af8ec82413836a76333ce0c2076961ad13f9cc3dc08b3989b95e9",
    "jobs": "c853c98be14913bfc4e739cbcaae82491f034aa509f136a30954ed6d5580f37f",
    "tests": "2a89a69148efcb67fc336da04c927dc2cf5ced4aaec9b0d176aa5f4e2014fbde",
    "psql": "256981b6ac3fd1f2105df4ffdae85e4d9e1a5f70d3874535122753f61afeb5e3",
    "watching": "1500647eb44f8b12be718b6dda83d48427492fff6b6cddcbfb8106adcb0612be",
    "odoo_15": "2d3110ec58af1f02ebdc4fd22bfbf0418703ccfd0dd13301d86a3a55d0fff6be",
    "odoo_16": "f8246fbfb092aebc8498298a2a1fbc998880fbcdfaf9c1d816e7e22a3898fc51",
    "odoo_17": "02bcff58f5cd21485902f30945ee927277f4b8c830bcea850605ec22f613b046",
    "odoo_18": "1437b307d5e7f455b6ee2eec06130e38a466e19fea65dd08d6a096bafdb09400",
    "odoo_19": "9f4be2bce348c7facb9b2451413c04b5b4ab57eb575f6edab1987cd530a6f680",
    "odoo_20": "f90edf35a6dfcc1b4f45938251bdfb797fb9dcfc01a43dd598664f682ee7c5cf",
}


def sha(text: str) -> str:

    return hashlib.sha256(text.encode()).hexdigest()


async def test_an_mcp_client_reads_every_text_as_it_did_before():
    assert sha(server.mcp.instructions) == MCP_GOLDEN["instructions"]
    topics = (await server.os_help())["topics"]
    assert set(topics) == set(MCP_GOLDEN) - {"instructions"}, "a topic came or went"
    changed = [
        topic for topic in topics
        if sha((await server.os_help(topic))["text"]) != MCP_GOLDEN[topic]
    ]
    assert changed == [], f"an MCP client would now read these differently: {changed}"


async def test_a_session_hands_over_the_same_rules_text(monkeypatch):
    monkeypatch.setattr(server, "_rules_given", set())
    described = server._with_rules({"odoo": "17.0"})
    assert sha(described["rules"]["text"]) == MCP_GOLDEN["odoo_17"]


def served_texts() -> dict[str, str]:
    """Every text as the daemon keeps it, markers and all."""
    texts = {"instructions": guidance.INSTRUCTIONS}
    for topic in guidance.topic_names():
        texts[topic] = guidance.text_of(topic)[1]

    return texts


async def test_the_markers_are_the_mcp_tools_without_their_prefix():
    """A marker an MCP client cannot resolve would reach it as `{{name}}`, and
    a tool with no marker is one an HTTP client is never told to substitute."""
    tools = {tool.name for tool in await server.mcp.list_tools()}
    assert {guidance.MCP_PREFIX + name for name in guidance.MARKERS} == tools


def test_every_marker_in_a_text_names_a_tool():
    unknown = {
        (topic, name)
        for topic, text in served_texts().items()
        for name in re.findall(r"\{\{(.*?)\}\}", text)
        if name not in guidance.MARKERS
    }
    assert unknown == set()


def test_no_marker_survives_the_mcp_rendering():
    for topic, text in served_texts().items():
        assert "{{" not in guidance.render_for_mcp(text), topic


def test_the_served_text_names_no_mcp_tool():
    """An HTTP client has its own verbs; an `os_` literal left in a text is a
    name it cannot call and was never told to replace."""
    for topic, text in served_texts().items():
        assert re.findall(r"\bos_\w+", text) == [], topic


def test_the_rules_files_carry_markers_too():
    """`common.md` names tools as well, and it goes in front of every major."""
    assert "{{run_test}}" in rules.common()


def test_the_tests_topic_says_where_to_see_a_browser_suite_cannot_run():
    tests = " ".join(server.HELP["tests"].split())
    for says in ("os_session", "js_tests", "chrome_not_found", "js_tests_skipped", "not a pass"):
        assert says in tests, says


# --- Odoo's own commits are welcome -----------------------------------------
#
# An agent kept declining `button_immediate_upgrade` because the call ends in a
# commit and its session had none granted. The gate is on the commit tool; it
# is not a veto on Odoo's own standard actions. The one place an agent is told
# to read and not write is someone else's instance.


def test_the_instructions_welcome_odoos_own_install_and_upgrade():
    text = server.INSTRUCTIONS
    assert "install" in text and "upgrade" in text
    assert "ask" in text.lower(), "the agent is told not to ask first"
    # the old line said nothing persists until a commit, which is what made an
    # agent read Odoo's own commit as a breach
    assert "Nothing persists until a commit" not in text


async def test_the_commit_topic_says_the_gate_is_not_a_veto_on_odoo():
    text = " ".join((await server.os_help("commit"))["text"].split())
    assert "button_immediate_upgrade" in text
    assert "commit_not_allowed" in text
    assert "not a way around" in text and "welcome" in text
    assert "someone else's instance" in text


async def test_the_modules_topic_calls_the_commit_odoos_own_and_welcome():
    # hard-wrapped prose: compare on the words, not on the wrapping
    text = " ".join((await server.os_help("modules"))["text"].split())
    assert "Odoo's own" in text and "welcome" in text
    assert "not a trick around the commit gate" in text
    assert "someone else's instance" in text
    # no refusal or hedge because a commit comes at the end
    assert "no reason to refuse or hedge" in text


# --- ...and that commit takes the whole transaction --------------------------
#
# `_button_immediate_function` commits the session's own cursor
# (odoo/addons/base/models/ir_module.py:638 in 19), so whatever the agent left
# uncommitted goes in with the module: a write no grant ever covered. Telling
# an agent to run them without asking made that the easy path. Roll back first.


def test_the_instructions_say_to_roll_back_before_install_or_upgrade():
    text = " ".join(server.INSTRUCTIONS.split())
    assert "commit the whole transaction" in text
    assert re.search(r"os_rollback[^.]*before Odoo's own `button_immediate_install`", text)


async def test_the_commit_topic_says_install_and_upgrade_take_whatever_is_uncommitted():
    text = " ".join((await server.os_help("commit"))["text"].split())
    assert "whole transaction" in text
    assert "os_rollback first" in text


async def test_the_modules_topic_says_to_roll_back_first():
    text = " ".join((await server.os_help("modules"))["text"].split())
    assert "commits the session's whole transaction" in text
    assert "os_rollback first" in text
    assert "a write no grant covered" in text
