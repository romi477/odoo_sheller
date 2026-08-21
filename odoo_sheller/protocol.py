"""Frame encoding for the daemon side of the pipe.

One JSON object per line, both directions. The bootstrap does not import this
module — it runs in a foreign interpreter and repeats the two lines it needs.
"""

import json

PROTOCOL_VERSION = 1

# Keep in lockstep with bootstrap._OS_MAX_STDOUT / _OS_MAX_RESULT. A clipped
# frame is still larger than asyncio's default 64 KiB StreamReader limit; if
# readline dies, the session stays busy forever and SIGINT is a no-op.
MAX_STDOUT = 1_000_000
MAX_RESULT = 100_000
FRAME_LINE_LIMIT = MAX_STDOUT + MAX_RESULT + 65_536


class ProtocolError(Exception):
    """A line on the pipe was not a usable frame."""


def encode_frame(frame: dict) -> str:

    return json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n"


def decode_frame(line: str) -> dict:
    text = line.strip()
    if not text:
        raise ProtocolError("empty line")
    try:
        frame = json.loads(text)
    except ValueError as exc:
        raise ProtocolError(f"not JSON: {exc}") from exc
    if not isinstance(frame, dict):
        raise ProtocolError(f"frame is {type(frame).__name__}, not an object")
    if "t" not in frame:
        raise ProtocolError("frame has no type")

    return frame


def exec_frame(request_id: int, code: str) -> dict:

    return {"t": "exec", "id": request_id, "code": code}


def commit_frame(request_id: int) -> dict:

    return {"t": "commit", "id": request_id}


def rollback_frame(request_id: int) -> dict:

    return {"t": "rollback", "id": request_id}


def close_frame(request_id: int) -> dict:

    return {"t": "close", "id": request_id}
