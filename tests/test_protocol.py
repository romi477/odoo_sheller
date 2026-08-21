import json

import pytest

from odoo_sheller import protocol


def test_encode_frame_is_one_line_of_json():
    line = protocol.encode_frame({"t": "exec", "id": 1, "code": "print('a\nb')"})
    assert line.endswith("\n")
    assert line.count("\n") == 1
    assert json.loads(line)["code"] == "print('a\nb')"


def test_decode_frame_roundtrip():
    frame = {"t": "result", "id": 7, "stdout": "line1\nline2", "result": None}
    assert protocol.decode_frame(protocol.encode_frame(frame)) == frame


def test_decode_frame_keeps_non_ascii_readable():
    line = protocol.encode_frame({"t": "result", "id": 1, "stdout": "Ошибка"})
    assert "Ошибка" in line


@pytest.mark.parametrize("line", ["", "   \n", "not json", "[1, 2]", '{"id": 1}'])
def test_decode_frame_rejects_garbage(line):
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_frame(line)


def test_builders_carry_type_and_id():
    assert protocol.exec_frame(3, "1 + 1") == {"t": "exec", "id": 3, "code": "1 + 1"}
    assert protocol.commit_frame(4) == {"t": "commit", "id": 4}
    assert protocol.rollback_frame(5) == {"t": "rollback", "id": 5}
    assert protocol.close_frame(6) == {"t": "close", "id": 6}


def test_frame_line_limit_covers_a_clipped_bootstrap_payload():
    """64 KiB is asyncio's default; a clipped stdout+result frame is larger."""
    assert protocol.FRAME_LINE_LIMIT > 64 * 1024
    assert protocol.FRAME_LINE_LIMIT >= protocol.MAX_STDOUT + protocol.MAX_RESULT
