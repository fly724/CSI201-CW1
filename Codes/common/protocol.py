"""Wire protocol shared by the quiz server and the quiz client.

Frame layout (one application message):

    0               1               2               3
    +-------+-------+-------+-------+
    |        payload length N       |   unsigned 32-bit, network order
    +-------+-------+-------+-------+
    |        N bytes of UTF-8 JSON  |
    |              object           |
    +-------------------------------+

N counts payload bytes, never Unicode characters.  A frame is therefore
self-delimiting and can be recovered from an arbitrarily fragmented or
coalesced TCP byte stream (see :class:`FrameReader`).

Every payload is a single JSON object with the common envelope keys
``version``, ``type``, ``request_id``, ``event_id``, ``session_id`` and
``payload``.  Requests and direct responses carry ``request_id``; server
initiated events carry ``event_id``.  Exactly one of the two is non-null.
"""

from __future__ import annotations

import json
import struct
from typing import Any, Callable

PROTOCOL_VERSION = 1

FRAME_PREFIX_BYTES = 4
MAX_PAYLOAD_BYTES = 65536

# Envelope field names.
F_VERSION = "version"
F_TYPE = "type"
F_REQUEST_ID = "request_id"
F_EVENT_ID = "event_id"
F_SESSION_ID = "session_id"
F_PAYLOAD = "payload"

ENVELOPE_FIELDS = (F_VERSION, F_TYPE, F_REQUEST_ID, F_EVENT_ID, F_SESSION_ID, F_PAYLOAD)


class ProtocolError(Exception):
    """Base class for protocol violations.

    ``code`` matches the error code published to the peer; ``recoverable``
    tells the connection whether the byte stream is still aligned.
    """

    code = "INVALID_MESSAGE"
    recoverable = True

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class FramingError(ProtocolError):
    """Length prefix violation: the stream can no longer be trusted."""

    code = "INVALID_FRAME"
    recoverable = False


class DecodeError(ProtocolError):
    """Payload is not valid UTF-8, not JSON, or not a JSON object."""

    code = "INVALID_MESSAGE"
    recoverable = True


class EnvelopeError(ProtocolError):
    """Payload parsed, but the common envelope does not follow the contract."""

    code = "INVALID_MESSAGE"
    recoverable = True


def encode_message(message: dict[str, Any]) -> bytes:
    """Serialise one envelope to a length-prefixed frame."""
    if not isinstance(message, dict):
        raise TypeError("message must be a dict")
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not payload:
        raise ValueError("empty payload")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("payload exceeds the maximum frame size")
    return struct.pack(">I", len(payload)) + payload


def build_envelope(
    message_type: str,
    payload: dict[str, Any] | None = None,
    request_id: str | None = None,
    event_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Build a validated-by-construction envelope."""
    if request_id is not None and event_id is not None:
        raise ValueError("an envelope carries either a request_id or an event_id")
    return {
        F_VERSION: PROTOCOL_VERSION,
        F_TYPE: message_type,
        F_REQUEST_ID: request_id,
        F_EVENT_ID: event_id,
        F_SESSION_ID: session_id,
        F_PAYLOAD: dict(payload or {}),
    }


def build_request(message_type: str, payload: dict[str, Any] | None, request_id: str) -> dict[str, Any]:
    return build_envelope(message_type, payload, request_id=request_id)


def build_response(message_type: str, payload: dict[str, Any] | None, request_id: str | None,
                   session_id: str | None = None) -> dict[str, Any]:
    return build_envelope(message_type, payload, request_id=request_id, session_id=session_id)


def build_event(message_type: str, payload: dict[str, Any] | None, event_id: str,
                session_id: str | None = None) -> dict[str, Any]:
    return build_envelope(message_type, payload, event_id=event_id, session_id=session_id)


def build_error(error_code: str, message: str, request_id: str | None = None,
                session_id: str | None = None) -> dict[str, Any]:
    return build_response(
        "ERROR_RESPONSE",
        {"code": error_code, "message": message},
        request_id=request_id,
        session_id=session_id,
    )


def validate_envelope(message: Any) -> tuple[str, str | None, dict[str, Any]]:
    """Validate the common envelope and return ``(type, request_id, payload)``.

    Unknown *extra* envelope keys are tolerated for forward compatibility;
    missing or wrongly typed required keys are not.
    """
    if not isinstance(message, dict):
        raise EnvelopeError("application message must be a JSON object")

    for field_name in ENVELOPE_FIELDS:
        if field_name not in message:
            raise EnvelopeError(f"missing envelope field {field_name!r}")

    version = message[F_VERSION]
    if isinstance(version, bool) or not isinstance(version, int):
        raise EnvelopeError("envelope field 'version' must be an integer")
    if version != PROTOCOL_VERSION:
        raise EnvelopeError(f"unsupported protocol version {version!r}")

    message_type = message[F_TYPE]
    if not isinstance(message_type, str) or not message_type:
        raise EnvelopeError("envelope field 'type' must be a non-empty string")
    if len(message_type) > 64 or not message_type.replace("_", "").isalnum():
        raise EnvelopeError("envelope field 'type' has an illegal value")

    request_id = message[F_REQUEST_ID]
    if request_id is not None and not isinstance(request_id, str):
        raise EnvelopeError("envelope field 'request_id' must be a string or null")
    if isinstance(request_id, str) and len(request_id) > 64:
        raise EnvelopeError("envelope field 'request_id' is too long")

    event_id = message[F_EVENT_ID]
    if event_id is not None and not isinstance(event_id, str):
        raise EnvelopeError("envelope field 'event_id' must be a string or null")
    if isinstance(event_id, str) and len(event_id) > 64:
        raise EnvelopeError("envelope field 'event_id' is too long")
    if request_id is not None and event_id is not None:
        raise EnvelopeError("an envelope carries either a request_id or an event_id")

    session_id = message[F_SESSION_ID]
    if session_id is not None and not isinstance(session_id, str):
        raise EnvelopeError("envelope field 'session_id' must be a string or null")

    payload = message[F_PAYLOAD]
    if not isinstance(payload, dict):
        raise EnvelopeError("envelope field 'payload' must be a JSON object")

    return message_type, request_id, payload


class FrameReader:
    """Incremental frame decoder for one TCP connection.

    The reader owns a per-connection buffer.  ``feed`` never assumes a
    relationship between ``recv`` calls and application messages: one call may
    deliver a fragment of a frame, several complete frames, or any mix.
    """

    __slots__ = ("_buffer", "max_payload_bytes")

    def __init__(self, max_payload_bytes: int = MAX_PAYLOAD_BYTES):
        self._buffer = bytearray()
        self.max_payload_bytes = max_payload_bytes

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes, sink: Callable[[dict[str, Any]], None]) -> int:
        """Append ``data`` and hand every complete envelope to ``sink``.

        Returns the number of messages dispatched.  Raises :class:`FramingError`
        or :class:`DecodeError`; a decode error leaves the stream aligned, a
        framing error does not, so the caller closes the connection.
        """
        if not data:
            return 0
        self._buffer.extend(data)
        dispatched = 0
        while True:
            if len(self._buffer) < FRAME_PREFIX_BYTES:
                return dispatched
            payload_length = int.from_bytes(self._buffer[:FRAME_PREFIX_BYTES], "big")
            if payload_length == 0:
                self._buffer.clear()
                raise FramingError("zero-length payload is not a valid frame")
            if payload_length > self.max_payload_bytes:
                self._buffer.clear()
                raise FramingError(
                    f"advertised payload length {payload_length} exceeds the "
                    f"maximum of {self.max_payload_bytes} bytes"
                )
            frame_end = FRAME_PREFIX_BYTES + payload_length
            if len(self._buffer) < frame_end:
                return dispatched
            raw_payload = bytes(self._buffer[FRAME_PREFIX_BYTES:frame_end])
            del self._buffer[:frame_end]
            try:
                text = raw_payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise DecodeError(f"payload is not valid UTF-8 ({exc.reason})") from None
            try:
                message = json.loads(text)
            except json.JSONDecodeError as exc:
                raise DecodeError(f"payload is not valid JSON ({exc.msg})") from None
            except RecursionError:
                # Deeply nested documents exhaust the decoder's stack; treat them
                # as a bad payload instead of letting the exception escape the
                # event loop (a 64 KiB frame can nest thousands of levels).
                raise DecodeError("payload nesting is too deep") from None
            if not isinstance(message, dict):
                raise DecodeError("payload must be a JSON object")
            sink(message)
            dispatched += 1
