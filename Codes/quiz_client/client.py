"""Command-line quiz client.

The client is deliberately thin: it renders the server's view of the world,
translates a small command vocabulary into protocol requests and prints every
server event it receives.  It never reads the question-bank file, never
computes scores and never assumes when a question closes.

Threading: the main thread owns stdin and the socket for writing; one daemon
receiver thread owns reading and printing.  Only the main thread writes, so no
lock is required around the socket.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from typing import Any

from common.protocol import (
    DecodeError,
    FramingError,
    FrameReader,
    build_request,
    encode_message,
    validate_envelope,
)

PROMPT = "> "

HELP_TEXT = """Commands
  /quizzes            list the quiz catalogue
  /rooms              list joinable rooms
  /create <quiz_id>   create a room and become its host
  /join <room_code>   join an existing room
  /players            show participants and readiness
  /ready              mark yourself ready
  /notready           mark yourself not ready
  /start              host starts the quiz
  /answer <A|B|C|D>   answer the open question
  /score              show the current leaderboard
  /leave              leave the current room
  /status             show your user id, room and whether a question is open
  /quit               log out and close the connection
  /help               show this list

While a question is open you may also answer with a bare letter: A, B, C or D.
"""


class ClientError(Exception):
    pass


class QuizClient:
    def __init__(self, host: str, port: int, user_id: str, display_name: str, *, connect_timeout: float = 5.0):
        self.host = host
        self.port = port
        self.user_id = user_id
        self.display_name = display_name
        self.connect_timeout = connect_timeout
        self.sock: socket.socket | None = None
        self.reader = FrameReader()
        self._request_counter = 0
        self._print_lock = threading.Lock()
        self._closed = threading.Event()
        self._logged_in = False
        self._session_id: str | None = None
        self._room_code: str | None = None
        self._quiz_title: str | None = None
        self._open_question: dict[str, Any] | None = None
        self._receiver: threading.Thread | None = None
        self._exit_code = 0
        self._pending: dict[str, str] = {}

    # ------------------------------------------------------------------ output

    def _print(self, text: str = "") -> None:
        with self._print_lock:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    # --------------------------------------------------------------- transport

    def connect(self) -> None:
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except OSError as exc:
            raise ClientError(f"cannot connect to {self.host}:{self.port}: {exc}") from None
        self.sock.settimeout(None)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._receiver = threading.Thread(target=self._receive_loop, name="quiz-client-receiver", daemon=True)
        self._receiver.start()

    def _next_request_id(self) -> str:
        self._request_counter += 1
        return f"req-{self._request_counter:04d}"

    def request(self, message_type: str, payload: dict[str, Any] | None = None) -> str:
        if self.sock is None:
            raise ClientError("not connected")
        request_id = self._next_request_id()
        self._pending[request_id] = message_type
        message = build_request(message_type, payload, request_id)
        message["session_id"] = self._session_id
        try:
            self.sock.sendall(encode_message(message))
        except OSError as exc:
            raise ClientError(f"connection lost while sending: {exc}") from None
        return request_id

    def _receive_loop(self) -> None:
        assert self.sock is not None
        try:
            while not self._closed.is_set():
                try:
                    chunk = self.sock.recv(65536)
                except OSError as exc:
                    if not self._closed.is_set():
                        self._print(f"[CONNECTION] lost: {exc}")
                    break
                if not chunk:
                    if not self._closed.is_set():
                        self._print("[CONNECTION] closed by server")
                    self._closed.set()
                    break
                try:
                    self.reader.feed(chunk, self._handle_message)
                except (FramingError, DecodeError) as exc:
                    self._print(f"[PROTOCOL] {exc.message}")
                    self._closed.set()
                    break
        finally:
            self._closed.set()

    # ----------------------------------------------------------- message output

    def _handle_message(self, message: dict[str, Any]) -> None:
        try:
            message_type, request_id, payload = validate_envelope(message)
        except Exception as exc:  # envelope problems are reported, never fatal
            self._print(f"[PROTOCOL] rejected message: {exc}")
            return
        handler = getattr(self, f"_on_{message_type.lower()}", None)
        if handler is not None:
            handler(message_type, request_id, payload)
        else:
            self._print(f"[MESSAGE] {message_type} {payload}")

    # -- responses -------------------------------------------------------------

    def _on_login_response(self, message_type: str, request_id: str | None, payload: dict[str, Any]) -> None:
        self._logged_in = bool(payload.get("success"))
        self._session_id = payload.get("session_id")
        self._print(f"[LOGIN] connected as {payload.get('name')} (user_id={payload.get('user_id')})")
        self._print("Type /help for the command list.")

    def _on_list_quizzes_response(self, message_type: str, request_id: str | None, payload: dict[str, Any]) -> None:
        quizzes = payload.get("quizzes") or []
        self._print(f"[QUIZZES] {len(quizzes)} available")
        for index, quiz in enumerate(quizzes, start=1):
            description = quiz.get("description") or ""
            self._print(f"  {index}. {quiz.get('quiz_id')}  |  {quiz.get('title')}"
                        f"  ({quiz.get('question_count')} questions)")
            if description:
                self._print(f"     {description}")

    def _on_list_rooms_response(self, message_type: str, request_id: str | None, payload: dict[str, Any]) -> None:
        rooms = payload.get("rooms") or []
        if not rooms:
            self._print("[ROOMS] no joinable rooms right now")
            return
        self._print(f"[ROOMS] {len(rooms)} joinable")
        for room in rooms:
            self._print(f"  {room.get('room_code')}  |  {room.get('quiz_title')}  |  host {room.get('host_name')}"
                        f"  |  {room.get('player_count')}/{room.get('max_players')} players")
            names = ", ".join(room.get("players") or [])
            if names:
                self._print(f"     players: {names}")

    def _on_create_room_response(self, message_type: str, request_id: str | None, payload: dict[str, Any]) -> None:
        self._room_code = payload.get("room_code")
        room = payload.get("room") or {}
        self._quiz_title = room.get("quiz_title")
        self._print(f"[ROOM] created {self._room_code} for quiz {room.get('quiz_id')}")
        self._render_players(room)

    def _on_join_room_response(self, message_type: str, request_id: str | None, payload: dict[str, Any]) -> None:
        self._room_code = payload.get("room_code")
        room = payload.get("room") or {}
        self._quiz_title = room.get("quiz_title")
        self._print(f"[ROOM] joined {self._room_code} ({room.get('quiz_title')})")
        self._render_players(room)

    def _render_players(self, room: dict[str, Any]) -> None:
        players = room.get("players") or []
        self._print(f"[PLAYERS] {len(players)} in room (state {room.get('state')})")
        for player in players:
            tags = []
            if player.get("is_host"):
                tags.append("host")
            tags.append("ready" if player.get("ready") else "not ready")
            self._print(f"  - {player.get('name')} (user_id={player.get('user_id')}) [{', '.join(tags)}]")

    def _on_player_list_response(self, message_type: str, request_id: str | None, payload: dict[str, Any]) -> None:
        self._render_players(payload)

    def _on_ready_response(self, message_type: str, request_id: str | None, payload: dict[str, Any]) -> None:
        self._print(f"[READY] you are now {'ready' if payload.get('ready') else 'not ready'}")

    def _on_start_quiz_response(self, message_type: str, request_id: str | None, payload: dict[str, Any]) -> None:
        self._print(f"[START] accepted: {payload.get('total_questions')} questions")

    def _on_answer_response(self, message_type: str, request_id: str | None, payload: dict[str, Any]) -> None:
        self._clear_question(payload)
        self._print(f"[ANSWER] accepted for question {payload.get('question_id')} "
                    f"(question {payload.get('number')})")

    def _on_leaderboard_response(self, message_type: str, request_id: str | None, payload: dict[str, Any]) -> None:
        self._print(f"[SCORE] room {payload.get('room_code')} (state {payload.get('state')})")
        self._render_leaderboard(payload.get("leaderboard") or [])

    def _on_leave_room_response(self, message_type: str, request_id: str | None, payload: dict[str, Any]) -> None:
        self._print(f"[ROOM] left {payload.get('room_code')}")
        self._room_code = None
        self._open_question = None

    def _on_logout_response(self, message_type: str, request_id: str | None, payload: dict[str, Any]) -> None:
        self._print("[LOGOUT] session closed")
        self._closed.set()

    def _on_error_response(self, message_type: str, request_id: str | None, payload: dict[str, Any]) -> None:
        self._print(f"[ERROR] {payload.get('code')}: {payload.get('message')}")

    # -- events ----------------------------------------------------------------

    def _on_player_status_event(self, message_type: str, request_id: str | None, payload: dict[str, Any]) -> None:
        self._render_players(payload)

    def _on_room_closed_event(self, message_type: str, request_id: str | None, payload: dict[str, Any]) -> None:
        self._print(f"[ROOM] {payload.get('room_code')} closed: {payload.get('reason')}")
        self._room_code = None
        self._open_question = None

    def _on_quiz_started_event(self, message_type: str, request_id: str | None, payload: dict[str, Any]) -> None:
        self._quiz_title = payload.get("title")
        self._print("")
        self._print("=" * 72)
        self._print(f"  QUIZ STARTED: {payload.get('title')} - {payload.get('total_questions')} questions")
        self._print("=" * 72)

    def _on_question_event(self, message_type: str, request_id: str | None, payload: dict[str, Any]) -> None:
        self._open_question = payload
        self._print("")
        self._print(f"[QUESTION {payload.get('number')}/{payload.get('total_questions')}] "
                    f"{payload.get('question_id')}  ({payload.get('time_limit')}s, {payload.get('points')} points)")
        self._print(f"  {payload.get('text')}")
        options = payload.get("options") or {}
        letters = sorted(options)
        for key in letters:
            self._print(f"  {key}) {options[key]}")
        self._print(f"  answer with /answer <{'|'.join(letters)}> within {payload.get('time_limit')}s")

    def _clear_question(self, payload: dict[str, Any]) -> None:
        question = self._open_question
        if (question is not None and question.get("question_id") == payload.get("question_id")
                and question.get("run_id") == payload.get("run_id")):
            self._open_question = None

    def _on_question_result_event(self, message_type: str, request_id: str | None, payload: dict[str, Any]) -> None:
        self._clear_question(payload)
        correct = payload.get("correct_answer")
        self._print(f"[RESULT {payload.get('number')}/{payload.get('total_questions')}] "
                    f"{payload.get('question_id')} correct answer: {correct}")
        for entry in payload.get("results") or []:
            if entry.get("user_id") == self.user_id:
                given = entry.get("your_answer") or "no answer"
                self._print(f"  you answered {given}; +{entry.get('points_awarded')} "
                            f"(total {entry.get('score')})")
        self._render_leaderboard(payload.get("leaderboard") or [])

    def _on_quiz_finished_event(self, message_type: str, request_id: str | None, payload: dict[str, Any]) -> None:
        self._print("")
        self._print("=" * 72)
        winners = payload.get("winner_user_ids") or []
        if payload.get("tied"):
            self._print(f"  QUIZ FINISHED: tie on {payload.get('winner_score')} points: {', '.join(winners)}")
        elif winners:
            self._print(f"  QUIZ FINISHED: winner {winners[0]} with {payload.get('winner_score')} points")
        else:
            self._print("  QUIZ FINISHED")
        self._print("=" * 72)
        self._render_leaderboard(payload.get("leaderboard") or [])

    def _on_host_changed_event(self, message_type: str, request_id: str | None, payload: dict[str, Any]) -> None:
        self._print(f"[ROOM] host is now {payload.get('host_name')}")

    def _render_leaderboard(self, leaderboard: list[dict[str, Any]]) -> None:
        if not leaderboard:
            self._print("  (leaderboard empty)")
            return
        for entry in leaderboard:
            self._print(f"  {entry.get('rank')}. {entry.get('name')} (user_id={entry.get('user_id')}) "
                        f"- {entry.get('score')} points, {entry.get('correct_count')} correct")

    # ------------------------------------------------------------------ session

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def logout(self) -> None:
        if self.sock is not None and not self._closed.is_set():
            try:
                self.request("LOGOUT_REQUEST")
                time.sleep(0.15)
            except ClientError:
                pass

    def close(self) -> None:
        self._closed.set()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # --------------------------------------------------------------- command loop

    def run(self) -> int:
        self._print(HELP_TEXT.rstrip())
        while not self._closed.is_set():
            try:
                line = input(PROMPT)
            except EOFError:
                self._print("")
                self.logout()
                break
            except KeyboardInterrupt:
                self._print("\n[CLIENT] interrupted, closing")
                self.logout()
                self._exit_code = 130
                break
            line = line.strip()
            if not line:
                continue
            try:
                if not self._handle_command(line):
                    break
            except ClientError as exc:
                self._print(f"[CLIENT] {exc}")
            except Exception as exc:  # a bad command must not kill the client
                self._print(f"[CLIENT] command failed: {exc}")
        self.close()
        return self._exit_code

    def _handle_command(self, line: str) -> bool:
        """Handle one line of input.  Returns False when the client should exit."""
        question = self._open_question
        if not line.startswith("/"):
            upper = line.upper()
            if upper in ("A", "B", "C", "D") and question is not None:
                self._submit_answer(question, upper)
                return True
            if line.isdigit() and question is not None and 1 <= int(line) <= 4:
                letter = "ABCD"[int(line) - 1]
                self._submit_answer(question, letter)
                return True
            self._print("[CLIENT] commands start with '/'. Type /help for the list.")
            return True

        parts = line.split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""

        if command == "/help":
            self._print(HELP_TEXT.rstrip())
        elif command == "/quizzes":
            self.request("LIST_QUIZZES_REQUEST")
        elif command == "/rooms":
            self.request("LIST_ROOMS_REQUEST")
        elif command == "/create":
            if not argument:
                self._print("[CLIENT] usage: /create <quiz_id>")
            else:
                self.request("CREATE_ROOM_REQUEST", {"quiz_id": argument})
        elif command == "/join":
            if not argument:
                self._print("[CLIENT] usage: /join <room_code>")
            else:
                self.request("JOIN_ROOM_REQUEST", {"room_code": argument})
        elif command == "/players":
            self.request("PLAYER_LIST_REQUEST")
        elif command in ("/ready", "/notready"):
            self.request("READY_REQUEST", {"ready": command == "/ready"})
        elif command == "/start":
            self.request("START_QUIZ_REQUEST")
        elif command == "/answer":
            answer = argument.strip().upper()
            if not answer:
                self._print("[CLIENT] usage: /answer <A|B|C|D>")
            elif question is None:
                self._print("[CLIENT] no question is open")
            else:
                self._submit_answer(question, answer)
        elif command == "/score":
            self.request("LEADERBOARD_REQUEST")
        elif command == "/leave":
            self.request("LEAVE_ROOM_REQUEST")
        elif command == "/quit":
            self.logout()
            return False
        elif command == "/status":
            self._print(f"[STATUS] user_id={self.user_id} room={self._room_code} "
                        f"question_open={self._open_question is not None}")
        else:
            self._print(f"[CLIENT] unknown command {command!r}. Type /help for the list.")
        return True

    def _submit_answer(self, question: dict[str, Any], answer: str) -> None:
        self.request("ANSWER_REQUEST", {
            "question_id": question.get("question_id"),
            "run_id": question.get("run_id"),
            "answer": answer,
        })
