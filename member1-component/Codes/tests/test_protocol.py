"""Unit tests for framing and envelope validation."""

from __future__ import annotations

import json
import unittest

from quiznet.protocol import (
    DecodeError,
    EnvelopeError,
    FrameReader,
    FramingError,
    MAX_PAYLOAD_BYTES,
    build_error,
    build_event,
    build_request,
    build_response,
    encode_message,
    validate_envelope,
)


class FramingTests(unittest.TestCase):
    def test_round_trip(self):
        message = build_request("LIST_QUIZZES_REQUEST", {}, "req-0001")
        frame = encode_message(message)
        reader = FrameReader()
        received = []
        reader.feed(frame, received.append)
        self.assertEqual(received, [message])

    def test_fragmented_frame_is_decoded_once(self):
        message = build_request("LOGIN_REQUEST", {"user_id": "alice", "name": "Alice"}, "req-0001")
        frame = encode_message(message)
        reader = FrameReader()
        received = []
        for index in range(len(frame)):
            reader.feed(frame[index:index + 1], received.append)
        self.assertEqual(received, [message])
        self.assertEqual(reader.buffered_bytes, 0)

    def test_coalesced_frames_are_decoded_in_order(self):
        messages = [build_request("LIST_ROOMS_REQUEST", {}, f"req-{n}") for n in range(3)]
        blob = b"".join(encode_message(message) for message in messages)
        reader = FrameReader()
        received = []
        reader.feed(blob, received.append)
        self.assertEqual(len(received), 3)
        self.assertEqual([m["request_id"] for m in received], ["req-0", "req-1", "req-2"])

    def test_partial_then_complete(self):
        message = build_response("LOGIN_RESPONSE", {"success": True}, "req-0001")
        frame = encode_message(message)
        reader = FrameReader()
        received = []
        reader.feed(frame[:2], received.append)
        self.assertEqual(received, [])
        reader.feed(frame[2:], received.append)
        self.assertEqual(len(received), 1)

    def test_zero_length_is_a_framing_error(self):
        reader = FrameReader()
        with self.assertRaises(FramingError):
            reader.feed(b"\x00\x00\x00\x00", lambda m: None)

    def test_oversized_frame_is_a_framing_error(self):
        reader = FrameReader()
        with self.assertRaises(FramingError):  # the header alone is enough to refuse
            reader.feed(b"\x00\x01\x00\x01", lambda m: None)  # 65537 > 65536

    def test_oversized_frame_does_not_buffer(self):
        reader = FrameReader()
        with self.assertRaises(FramingError):
            reader.feed(b"\x7f\xff\xff\xff", lambda m: None)
        self.assertEqual(reader.buffered_bytes, 0)

    def test_invalid_utf8_is_recoverable(self):
        reader = FrameReader()
        received = []
        payload = b'{"a": "\xff"}'
        bad = len(payload).to_bytes(4, "big") + payload
        with self.assertRaises(DecodeError):
            reader.feed(bad, received.append)
        good = encode_message(build_request("LIST_QUIZZES_REQUEST", {}, "req-1"))
        reader.feed(good, received.append)
        self.assertEqual(len(received), 1)

    def test_non_object_json_is_a_decode_error(self):
        reader = FrameReader()
        payload = b"[1, 2, 3]"
        with self.assertRaises(DecodeError):
            reader.feed(len(payload).to_bytes(4, "big") + payload, lambda m: None)

    def test_malformed_json_is_a_decode_error(self):
        reader = FrameReader()
        payload = b'{"type": '
        with self.assertRaises(DecodeError):
            reader.feed(len(payload).to_bytes(4, "big") + payload, lambda m: None)

    def test_deeply_nested_json_is_a_decode_error(self):
        reader = FrameReader()
        payload = b'{"a":' + b"[" * 12000 + b"]" * 12000 + b"}"
        with self.assertRaises(DecodeError):
            reader.feed(len(payload).to_bytes(4, "big") + payload, lambda m: None)

    def test_length_counts_bytes_not_characters(self):
        message = build_request("LOGIN_REQUEST", {"user_id": "héllo", "name": "Ünïcode"}, "req-1")
        frame = encode_message(message)
        self.assertEqual(int.from_bytes(frame[:4], "big"), len(frame) - 4)
        reader = FrameReader()
        received = []
        reader.feed(frame, received.append)
        self.assertEqual(received[0]["payload"]["name"], "Ünïcode")

    def test_payload_larger_than_protocol_limit_cannot_be_encoded(self):
        with self.assertRaises(ValueError):
            encode_message({"type": "X", "payload": {"data": "x" * (MAX_PAYLOAD_BYTES + 10)}})


class EnvelopeTests(unittest.TestCase):
    def test_valid_request(self):
        message = build_request("LIST_ROOMS_REQUEST", {}, "req-1")
        self.assertEqual(validate_envelope(message)[0], "LIST_ROOMS_REQUEST")

    def test_valid_event_has_no_request_id(self):
        message = build_event("QUESTION_EVENT", {"question_id": "Q1"}, "e1")
        self.assertEqual(validate_envelope(message)[0], "QUESTION_EVENT")

    def test_missing_field(self):
        message = build_request("LIST_ROOMS_REQUEST", {}, "req-1")
        del message["session_id"]
        with self.assertRaises(EnvelopeError):
            validate_envelope(message)

    def test_wrong_version(self):
        message = build_request("LIST_ROOMS_REQUEST", {}, "req-1")
        message["version"] = 2
        with self.assertRaises(EnvelopeError):
            validate_envelope(message)

    def test_request_and_event_id_together(self):
        message = build_request("LIST_ROOMS_REQUEST", {}, "req-1")
        message["event_id"] = "e1"
        with self.assertRaises(EnvelopeError):
            validate_envelope(message)

    def test_payload_must_be_object(self):
        message = build_request("LIST_ROOMS_REQUEST", {}, "req-1")
        message["payload"] = []
        with self.assertRaises(EnvelopeError):
            validate_envelope(message)

    def test_type_must_be_string(self):
        message = build_request("LIST_ROOMS_REQUEST", {}, "req-1")
        message["type"] = 7
        with self.assertRaises(EnvelopeError):
            validate_envelope(message)

    def test_error_envelope_helper(self):
        message = build_error("INVALID_SESSION", "log in first", request_id="req-9")
        self.assertEqual(message["type"], "ERROR_RESPONSE")
        self.assertEqual(message["payload"]["code"], "INVALID_SESSION")

    def test_unknown_extra_envelope_field_tolerated(self):
        message = build_request("LIST_ROOMS_REQUEST", {}, "req-1")
        message["trace"] = "abc"
        self.assertEqual(validate_envelope(message)[0], "LIST_ROOMS_REQUEST")


if __name__ == "__main__":
    unittest.main()
