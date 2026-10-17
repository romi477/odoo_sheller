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
    "instructions": "f535e62784a064db23a28f191649dbdfd2ae4b6bf39a7d7662190758862c3864",
    "sessions": "f279071f06d2a72b141caac228164e467205feedd8a355a352d5ca1dc2156bb5",
    "ownership": "a7dfdbc2da4f62f93ac40110271f76e3e3607b2b53ded4432ae37609bde313c6",
    "commit": "706b40b5cfc3ce94917e5a51b80f5b64fd72ca27ef3a969d6509ee7f8d70fc0c",
    "remote_server": "809bb8517bf1789aecb3d8ae60a5bcd3234b98e6441cce3d1a64d577ed3a316e",
    "limits": "2ed02929ffd28b600388968a63ffd8f1da64708b055117d424e448b832b78076",
    "orm": "29ce479be88e26e3c260c056cc2e71c2001be5069a1de0e8c6ef308a4aefd031",
    "code": "9695582a4342042586018028a87e617a9adffe8333083f67a3b4326914a14638",
    "log": "25be6d6b541c96978a501be05720c193444cdda9b29671724676c2673184b1a3",
    "records": "5b2c65b96dd3ba9f796619631e74644d1be58469e92cb2db1c8085c56db054a5",
    "modules": "0955570535534c2d4e6279884172dab9a8452c3fcab5dfcdd753f10856eecbca",
    "jobs": "c853c98be14913bfc4e739cbcaae82491f034aa509f136a30954ed6d5580f37f",
    "tests": "eeafa93ff0245854f859fac46250f862d661131c18c1792c6d25fbea79a2579c",
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
