"""Client-side tests for the Member 1 component.

Every test here is independent of the team server: the rendering tests call
``QuizClient._handle_message`` directly with envelopes produced by the protocol
module, and the end-to-end tests talk to ``tools/stub_server.py`` - the reference
peer that owns no quiz logic and exists only to exercise framing and
request/response correlation on the client side.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import threading
import time
import unittest

from quiznet.client import HELP_TEXT, QuizClient
from quiznet.protocol import build_error, build_event, build_response

CODES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUB = os.path.join(CODES_DIR, "tools", "stub_server.py")

COMMAND_WORDS = [
    "/help", "/quizzes", "/rooms", "/create", "/join", "/players", "/ready",
    "/notready", "/start", "/answer", "/score", "/leave", "/quit", "/status",
]


class RenderingTests(unittest.TestCase):
    """The client must render server messages without touching the socket."""

    def make_client(self) -> QuizClient:
        return QuizClient("127.0.0.1", 1, "alice", "Alice")

    def render(self, client: QuizClient, message: dict) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            client._handle_message(message)
        return buffer.getvalue()

    def test_help_text_lists_every_command(self) -> None:
        missing = [word for word in COMMAND_WORDS if word not in HELP_TEXT]
        self.assertEqual(missing, [])
        self.assertIn("bare letter", HELP_TEXT)

    def test_login_response_records_session(self) -> None:
        client = self.make_client()
        body = {"success": True, "session_id": "s-1", "user_id": "alice", "name": "Alice"}
        output = self.render(client, build_response("LOGIN_RESPONSE", body, "req-0001"))
        self.assertIn("[LOGIN] connected as Alice (user_id=alice)", output)
        self.assertTrue(client._logged_in)
        self.assertEqual(client._session_id, "s-1")

    def test_failed_login_is_not_logged_in(self) -> None:
        client = self.make_client()
        output = self.render(client, build_error("DUPLICATE_USER", "alice is already connected", "req-0002"))
        self.assertIn("[ERROR] DUPLICATE_USER: alice is already connected", output)
        self.assertFalse(client._logged_in)

    def test_quiz_catalogue_rendering(self) -> None:
        quizzes = [
            {"quiz_id": "stub-a", "title": "Stub Quiz A", "question_count": 5, "description": "first"},
            {"quiz_id": "stub-b", "title": "Stub Quiz B", "question_count": 3, "description": ""},
        ]
        output = self.render(self.make_client(),
                             build_response("LIST_QUIZZES_RESPONSE", {"quizzes": quizzes}, "req-0003"))
        self.assertIn("[QUIZZES] 2 available", output)
        self.assertIn("1. stub-a  |  Stub Quiz A  (5 questions)", output)
        self.assertIn("first", output)
        self.assertIn("2. stub-b  |  Stub Quiz B  (3 questions)", output)

    def test_empty_room_list_is_explicit(self) -> None:
        output = self.render(self.make_client(),
                             build_response("LIST_ROOMS_RESPONSE", {"rooms": []}, "req-0004"))
        self.assertIn("[ROOMS] no joinable rooms right now", output)

    def test_player_status_event_shows_readiness(self) -> None:
        payload = {
            "state": "LOBBY",
            "players": [
                {"user_id": "alice", "name": "Alice", "is_host": True, "ready": True},
                {"user_id": "bob", "name": "Bob", "is_host": False, "ready": False},
            ],
        }
        output = self.render(self.make_client(), build_event("PLAYER_STATUS_EVENT", payload, "evt-1"))
        self.assertIn("[PLAYERS] 2 in room (state LOBBY)", output)
        self.assertIn("- Alice (user_id=alice) [host, ready]", output)
        self.assertIn("- Bob (user_id=bob) [not ready]", output)

    def test_question_event_renders_options_and_tracks_state(self) -> None:
        payload = {
            "question_id": "q1",
            "number": 1,
            "total_questions": 5,
            "text": "Which port does DNS use by default?",
            "options": {"A": "53", "B": "80", "C": "443", "D": "25"},
            "time_limit": 15,
            "points": 100,
        }
        client = self.make_client()
        output = self.render(client, build_event("QUESTION_EVENT", payload, "evt-2"))
        self.assertIn("[QUESTION 1/5] q1  (15s, 100 points)", output)
        self.assertIn("Which port does DNS use by default?", output)
        self.assertIn("A) 53", output)
        self.assertIn("/answer <A|B|C|D>", output)
        self.assertEqual(client._open_question, payload)

    def test_question_result_closes_the_open_question(self) -> None:
        client = self.make_client()
        client._open_question = {"question_id": "q1"}
        payload = {
            "question_id": "q1",
            "number": 1,
            "total_questions": 5,
            "correct_answer": "A",
            "results": [
                {"user_id": "alice", "your_answer": "A", "points_awarded": 100, "score": 100},
                {"user_id": "bob", "your_answer": None, "points_awarded": 0, "score": 0},
            ],
            "leaderboard": [
                {"rank": 1, "user_id": "alice", "name": "Alice", "score": 100, "correct_count": 1},
            ],
        }
        output = self.render(client, build_event("QUESTION_RESULT_EVENT", payload, "evt-3"))
        self.assertIn("correct answer: A", output)
        self.assertIn("you answered A; +100 (total 100)", output)
        self.assertIn("1. Alice (user_id=alice) - 100 points, 1 correct", output)
        self.assertIsNone(client._open_question)

    def test_tie_is_reported_as_tie(self) -> None:
        payload = {
            "tied": True,
            "winner_user_ids": ["alice", "bob"],
            "winner_score": 200,
            "leaderboard": [
                {"rank": 1, "user_id": "alice", "name": "Alice", "score": 200, "correct_count": 2},
                {"rank": 1, "user_id": "bob", "name": "Bob", "score": 200, "correct_count": 2},
            ],
        }
        output = self.render(self.make_client(), build_event("QUIZ_FINISHED_EVENT", payload, "evt-4"))
        self.assertIn("tie on 200 points: alice, bob", output)

    def test_invalid_envelope_is_reported_and_survived(self) -> None:
        client = self.make_client()
        output = self.render(client, {"version": 99, "type": "LOGIN_RESPONSE"})
        self.assertIn("[PROTOCOL] rejected message:", output)
        follow_up = self.render(client, build_response("LOGIN_RESPONSE",
                                                       {"success": True, "session_id": "s", "user_id": "alice",
                                                        "name": "Alice"}, "req-0001"))
        self.assertIn("[LOGIN] connected as Alice", follow_up)

    def test_unknown_message_type_falls_back_to_dump(self) -> None:
        output = self.render(self.make_client(),
                             build_response("EXPERIMENTAL_RESPONSE", {"x": 1}, "req-0005"))
        self.assertIn("[MESSAGE] EXPERIMENTAL_RESPONSE", output)


class CommandTests(unittest.TestCase):
    """Commands must translate into the request types the team agreed."""

    def setUp(self) -> None:
        self.client = QuizClient("127.0.0.1", 1, "alice", "Alice")
        self.sent: list[tuple[str, dict]] = []
        self.client.request = lambda message_type, payload=None: self.sent.append((message_type, payload or {}))  # type: ignore[assignment]

    def run_command(self, line: str) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.client._handle_command(line)
        return buffer.getvalue()

    def test_slash_commands_map_to_requests(self) -> None:
        for line in ("/quizzes", "/rooms", "/players", "/start", "/score", "/leave"):
            self.run_command(line)
        self.assertEqual([entry[0] for entry in self.sent], [
            "LIST_QUIZZES_REQUEST", "LIST_ROOMS_REQUEST", "PLAYER_LIST_REQUEST",
            "START_QUIZ_REQUEST", "LEADERBOARD_REQUEST", "LEAVE_ROOM_REQUEST",
        ])

    def test_ready_toggle_and_arguments(self) -> None:
        self.run_command("/ready")
        self.run_command("/notready")
        self.run_command("/create stub-a")
        self.run_command("/join DEMO")
        self.assertEqual(self.sent[0], ("READY_REQUEST", {"ready": True}))
        self.assertEqual(self.sent[1], ("READY_REQUEST", {"ready": False}))
        self.assertEqual(self.sent[2], ("CREATE_ROOM_REQUEST", {"quiz_id": "stub-a"}))
        self.assertEqual(self.sent[3], ("JOIN_ROOM_REQUEST", {"room_code": "DEMO"}))

    def test_answer_requires_an_open_question(self) -> None:
        output = self.run_command("/answer B")
        self.assertIn("[CLIENT] no question is open", output)
        self.assertEqual(self.sent, [])

        self.client._open_question = {"question_id": "q7"}
        self.run_command("/answer b")
        self.run_command("C")
        self.run_command("3")
        self.assertEqual([entry[0] for entry in self.sent], ["ANSWER_REQUEST"] * 3)
        self.assertEqual(self.sent[0], ("ANSWER_REQUEST", {"question_id": "q7", "answer": "B"}))
        self.assertEqual(self.sent[2], ("ANSWER_REQUEST", {"question_id": "q7", "answer": "C"}))

    def test_non_command_text_is_rejected_politely(self) -> None:
        output = self.run_command("hello")
        self.assertIn("commands start with '/'", output)
        self.assertEqual(self.sent, [])

    def test_unknown_command_is_reported(self) -> None:
        output = self.run_command("/teleport")
        self.assertIn("unknown command '/teleport'", output)


class StubServerTests(unittest.TestCase):
    """End-to-end framing against the reference peer."""

    lines: list[str] = []

    @classmethod
    def setUpClass(cls) -> None:
        cls.process = subprocess.Popen(
            [sys.executable, "-W", "ignore::ResourceWarning", STUB],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=CODES_DIR,
        )
        first = cls.process.stdout.readline().strip()
        if not first.startswith("STUB_READY"):
            cls.process.kill()
            raise RuntimeError(f"stub server did not start: {first!r}")
        cls.port = int(first.split()[1])
        cls.reader = threading.Thread(target=cls._drain, name="stub-log-drain", daemon=True)
        cls.reader.start()

    @classmethod
    def _drain(cls) -> None:
        for line in cls.process.stdout:
            cls.lines.append(line.strip())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        finally:
            if cls.process.poll() is None:
                cls.process.kill()

    def saw_request(self, message_type: str, timeout: float = 3.0) -> bool:
        deadline = time.time() + timeout
        token = f"type={message_type} "
        while time.time() < deadline:
            if any(token in line for line in self.lines):
                return True
            time.sleep(0.02)
        return False

    def test_login_and_catalogue_round_trip(self) -> None:
        client = QuizClient("127.0.0.1", self.port, "alice", "Alice")
        client.connect()
        try:
            client.request("LOGIN_REQUEST", {"user_id": "alice", "name": "Alice"})
            client.request("LIST_QUIZZES_REQUEST")
            deadline = time.time() + 3.0
            while time.time() < deadline and not client._logged_in:
                time.sleep(0.02)
            self.assertTrue(client._logged_in, "no LOGIN_RESPONSE was decoded")
            self.assertIsNotNone(client._session_id, "session id must be kept for later requests")
            self.assertTrue(self.saw_request("LIST_QUIZZES_REQUEST"),
                            "stub never received the catalogue request")
        finally:
            client.close()

    def test_logout_closes_the_session(self) -> None:
        client = QuizClient("127.0.0.1", self.port, "bob", "Bob")
        client.connect()
        client.request("LOGIN_REQUEST", {"user_id": "bob", "name": "Bob"})
        deadline = time.time() + 3.0
        while time.time() < deadline and not client._logged_in:
            time.sleep(0.02)
        self.assertTrue(client._logged_in)
        client.logout()
        deadline = time.time() + 3.0
        while time.time() < deadline and not client.closed:
            time.sleep(0.02)
        self.assertTrue(client.closed)
        client.close()

    def test_error_response_does_not_close_the_socket(self) -> None:
        client = QuizClient("127.0.0.1", self.port, "carol", "Carol")
        client.connect()
        try:
            client.request("REPLAY_REQUEST", {"quiz_id": "stub-a"})
            self.assertTrue(self.saw_request("REPLAY_REQUEST"))
            time.sleep(0.2)
            self.assertFalse(client.closed, "a NOT_IMPLEMENTED error must not close the socket")
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
