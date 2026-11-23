"""Shared protocol layer: framing, envelope validation and message builders."""

from .protocol import (
    DecodeError,
    EnvelopeError,
    FrameReader,
    FramingError,
    MAX_PAYLOAD_BYTES,
    PROTOCOL_VERSION,
    build_error,
    build_event,
    build_request,
    build_response,
    encode_message,
    validate_envelope,
)

__all__ = [
    "DecodeError",
    "EnvelopeError",
    "FrameReader",
    "FramingError",
    "MAX_PAYLOAD_BYTES",
    "PROTOCOL_VERSION",
    "build_error",
    "build_event",
    "build_request",
    "build_response",
    "encode_message",
    "validate_envelope",
]
