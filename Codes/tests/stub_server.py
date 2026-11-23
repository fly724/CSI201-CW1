"""Reference peer used by Member 1 to exercise the client on its own.

This is **not** the assessed server.  The real server, its room state machine,
scoring and concurrency behaviour belong to Members 2-4.  What lives here is
the smallest possible peer that honours the wire contract in
``quiznet/protocol.py``: it decodes frames with the same ``FrameReader``, replies
with correctly correlated ``_RESPONSE`` envelopes and answers anything outside
Member 1's responsibility with a ``NOT_IMPLEMENTED`` error, so that a client
defect can never be mistaken for a server defect while the team is assembling
the real system.

    python3 tools/stub_server.py --port 5000

The port is printed as ``STUB_READY <port>`` once the listening socket is
bound (use ``--port 0`` for an ephemeral port in tests).
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # Codes/

from common.protocol import (  # noqa: E402
    DecodeError,
    EnvelopeError,
    FrameReader,
    FramingError,
    build_error,
    build_response,
    encode_message,
    validate_envelope,
)

QUIZZES = [
    {
        "quiz_id": "stub-a",
        "title": "Stub Quiz A",
        "description": "Canned catalogue entry used by the reference peer.",
        "question_count": 5,
    },
    {
        "quiz_id": "stub-b",
        "title": "Stub Quiz B",
        "description": "Second canned entry, three questions.",
        "question_count": 3,
    },
]

ROOM_TEMPLATE = {
    "room_code": "DEMO",
    "quiz_id": "stub-a",
    "quiz_title": "Stub Quiz A",
    "state": "LOBBY",
    "host_user_id": "stub-host",
    "players": [
        {"user_id": "stub-host", "name": "Stub Host", "is_host": True, "ready": False},
        {"user_id": "stub-peer", "name": "Stub Peer", "is_host": False, "ready": True},
    ],
}

LEADERBOARD = [
    {"rank": 1, "user_id": "stub-peer", "name": "Stub Peer", "score": 120, "correct_count": 2},
    {"rank": 2, "user_id": "stub-host", "name": "Stub Host", "score": 60, "correct_count": 1},
]


def _room() -> dict:
    import copy

    return copy.deepcopy(ROOM_TEMPLATE)


def handle(message_type: str, payload: dict, session: dict) -> tuple[dict | None, bool]:
    """Return ``(response, close_connection)`` for one decoded request."""
    if message_type == "LOGIN_REQUEST":
        session["user_id"] = payload.get("user_id") or "anonymous"
        session["name"] = payload.get("name") or session["user_id"]
        body = {
            "success": True,
            "session_id": session["session_id"],
            "user_id": session["user_id"],
            "name": session["name"],
        }
        return body, False
    if message_type == "LIST_QUIZZES_REQUEST":
        return {"quizzes": QUIZZES}, False
    if message_type == "LIST_ROOMS_REQUEST":
        return {"rooms": []}, False
    if message_type == "CREATE_ROOM_REQUEST":
        room = _room()
        room["quiz_id"] = payload.get("quiz_id") or "stub-a"
        return {"room_code": room["room_code"], "room": room}, False
    if message_type == "JOIN_ROOM_REQUEST":
        return {"room_code": payload.get("room_code") or "DEMO", "room": _room()}, False
    if message_type == "PLAYER_LIST_REQUEST":
        return _room(), False
    if message_type == "READY_REQUEST":
        return {"ready": bool(payload.get("ready"))}, False
    if message_type == "START_QUIZ_REQUEST":
        return {"total_questions": 5}, False
    if message_type == "ANSWER_REQUEST":
        return {"question_id": payload.get("question_id"), "number": 1}, False
    if message_type == "LEADERBOARD_REQUEST":
        return {"room_code": "DEMO", "state": "LOBBY", "leaderboard": LEADERBOARD}, False
    if message_type == "LEAVE_ROOM_REQUEST":
        return {"room_code": "DEMO"}, False
    if message_type == "LOGOUT_REQUEST":
        return {}, True
    return None, False


def serve_connection(conn: socket.socket, address: tuple, counter: int) -> None:
    session = {"session_id": f"stub-{counter:03d}"}
    reader = FrameReader()
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break

            def dispatch(message: dict) -> None:
                message_type, request_id, payload = validate_envelope(message)
                print(f"STUB request type={message_type} request_id={request_id}", flush=True)
                response, close = handle(message_type, payload, session)
                if response is None:
                    frame = build_error("NOT_IMPLEMENTED", f"{message_type} is not served by the stub",
                                        request_id=request_id)
                elif message_type == "LOGOUT_REQUEST":
                    frame = build_response("LOGOUT_RESPONSE", response, request_id, session["session_id"])
                else:
                    frame = build_response(message_type.replace("_REQUEST", "_RESPONSE"), response,
                                           request_id, session["session_id"])
                conn.sendall(encode_message(frame))
                if close:
                    raise _CloseAfterReply()

            try:
                reader.feed(chunk, dispatch)
            except _CloseAfterReply:
                break
            except (FramingError, DecodeError, EnvelopeError) as exc:
                print(f"STUB reject from {address}: {exc}", flush=True)
                break
    except OSError as exc:
        print(f"STUB connection error from {address}: {exc}", flush=True)
    finally:
        try:
            conn.close()
        except OSError:
            pass


class _CloseAfterReply(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stub_server.py", allow_abbrev=False)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args(argv)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((args.host, args.port))
        listener.listen(16)
        print(f"STUB_READY {listener.getsockname()[1]}", flush=True)
        counter = 0
        try:
            while True:
                conn, address = listener.accept()
                counter += 1
                threading.Thread(target=serve_connection, args=(conn, address, counter), daemon=True).start()
        except KeyboardInterrupt:
            print("STUB_STOPPED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
