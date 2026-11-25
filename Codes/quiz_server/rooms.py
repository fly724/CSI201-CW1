"""Room membership, quiz progression and scoring on the server event loop."""
import asyncio
from dataclasses import dataclass, field
import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quiz_server.network import Session

MAX_PLAYERS = 12
ROOM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class RequestError(ValueError):
    """The exception text is a public protocol error code."""


@dataclass
class Player:
    session: "Session"
    ready: bool = False
    score: int = 0
    correct_count: int = 0


@dataclass
class Room:
    code: str
    quiz: dict
    host_id: str
    players: dict = field(default_factory=dict)
    state: str = "LOBBY"
    run_id: int = 0
    question_index: int = -1
    answers: dict = field(default_factory=dict)
    deadline: float | None = None
    timer: object = None


class RoomService:
    def __init__(self, bank, emit):
        self.bank = bank
        self.emit = emit
        self.quizzes = {quiz["quiz_id"]: quiz for quiz in bank["quizzes"]}
        self.rooms = {}
        self.memberships = {}  # Session generation -> room, never a client-supplied identity.
        self._run_counter = 0
        self.handlers = {
            "CREATE_ROOM_REQUEST": self._create,
            "JOIN_ROOM_REQUEST": self._join,
            "LIST_ROOMS_REQUEST": self._list,
            "PLAYER_LIST_REQUEST": self._players,
            "READY_REQUEST": self._ready,
            "START_QUIZ_REQUEST": self._start,
            "ANSWER_REQUEST": self.submit_answer,
            "LEADERBOARD_REQUEST": self._leaderboard,
            "LEAVE_ROOM_REQUEST": self._leave,
        }

    def handle(self, session, kind, payload):
        handler = self.handlers.get(kind)
        if handler is None:
            raise RequestError("UNKNOWN_REQUEST")
        return handler(session, payload)

    @staticmethod
    def _text(payload, key):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RequestError("INVALID_REQUEST")
        return value.strip()

    def _room(self, session):
        code = self.memberships.get(session.session_id)
        if code is None:
            raise RequestError("NOT_IN_ROOM")
        return self.rooms[code]

    def _snapshot(self, room):
        host = room.players[room.host_id].session
        return {
            "room_code": room.code,
            "quiz_id": room.quiz["quiz_id"],
            "quiz_title": room.quiz["title"],
            "host_user_id": host.user_id,
            "host_name": host.name,
            "state": room.state,
            "max_players": MAX_PLAYERS,
            "players": [
                {"user_id": player.session.user_id, "name": player.session.name,
                 "ready": player.ready, "is_host": sid == room.host_id,
                 "score": player.score}
                for sid, player in room.players.items()
            ],
        }

    def _broadcast(self, room, kind, payload):
        for player in list(room.players.values()):
            # M2 schedules cleanup when emit returns False; it owns the connection.
            self.emit(player.session, kind, payload)

    def _notify_players(self, room):
        self._broadcast(room, "PLAYER_STATUS_EVENT", self._snapshot(room))

    def _add_player(self, room, session):
        room.players[session.session_id] = Player(session)
        self.memberships[session.session_id] = room.code

    def _create(self, session, payload):
        if session.session_id in self.memberships:
            raise RequestError("ALREADY_IN_ROOM")
        quiz = self.quizzes.get(self._text(payload, "quiz_id"))
        if quiz is None:
            raise RequestError("QUIZ_NOT_FOUND")
        code = "".join(secrets.choice(ROOM_ALPHABET) for _ in range(4))
        while code in self.rooms:
            code = "".join(secrets.choice(ROOM_ALPHABET) for _ in range(4))
        room = Room(code, quiz, session.session_id)
        self.rooms[code] = room
        self._add_player(room, session)
        self._notify_players(room)
        return {"room_code": code, "room": self._snapshot(room)}

    def _join(self, session, payload):
        if session.session_id in self.memberships:
            raise RequestError("ALREADY_IN_ROOM")
        code = self._text(payload, "room_code").upper()
        room = self.rooms.get(code)
        if room is None:
            raise RequestError("ROOM_NOT_FOUND")
        if room.state != "LOBBY":
            raise RequestError("GAME_IN_PROGRESS")
        if len(room.players) >= MAX_PLAYERS:
            raise RequestError("ROOM_FULL")
        self._add_player(room, session)
        self._notify_players(room)
        return {"room_code": code, "room": self._snapshot(room)}

    def _list(self, session, payload):
        return {"rooms": [
            {"room_code": room.code, "quiz_id": room.quiz["quiz_id"],
             "quiz_title": room.quiz["title"],
             "host_name": room.players[room.host_id].session.name,
             "player_count": len(room.players), "max_players": MAX_PLAYERS,
             "state": room.state,
             "players": [p.session.name for p in room.players.values()]}
            for room in self.rooms.values() if room.state == "LOBBY"
        ]}

    def _players(self, session, payload):
        return self._snapshot(self._room(session))

    def _ready(self, session, payload):
        room = self._room(session)
        if room.state not in ("LOBBY", "FINISHED"):
            raise RequestError("INVALID_ROOM_STATE")
        ready = payload.get("ready")
        if type(ready) is not bool:
            raise RequestError("INVALID_REQUEST")
        room.players[session.session_id].ready = ready
        self._notify_players(room)
        return {"ready": ready}

    def _start(self, session, payload):
        room = self._room(session)
        if room.host_id != session.session_id:
            raise RequestError("NOT_ROOM_HOST")
        if room.state not in ("LOBBY", "FINISHED"):
            raise RequestError("INVALID_ROOM_STATE")
        if len(room.players) < 2:
            raise RequestError("NOT_ENOUGH_PLAYERS")
        if not all(player.ready for player in room.players.values()):
            raise RequestError("PLAYERS_NOT_READY")
        self._run_counter += 1
        room.run_id = self._run_counter
        for player in room.players.values():
            player.score = 0
            player.correct_count = 0
            player.ready = False
        self._broadcast(room, "QUIZ_STARTED_EVENT", {
            "room_code": room.code, "quiz_id": room.quiz["quiz_id"],
            "title": room.quiz["title"], "total_questions": len(room.quiz["questions"]),
        })
        self.open_question(room.code, 0)
        return {"room_code": room.code, "total_questions": len(room.quiz["questions"])}

    def open_question(self, room_code, index):
        """Publish a question and schedule its deadline on the server event loop."""
        room = self.rooms[room_code]
        question = room.quiz["questions"][index]
        room.question_index = index
        room.answers.clear()
        room.state = "QUESTION_OPEN"
        loop = asyncio.get_running_loop()
        room.deadline = loop.time() + question["time_limit"]
        room.timer = loop.call_at(room.deadline, self.close_question,
                                  room.code, room.run_id, question["question_id"])
        public = {key: question[key] for key in ("question_id", "text", "time_limit", "points")}
        public["options"] = dict(question["options"])
        public.update(room_code=room.code, run_id=room.run_id, number=index + 1,
                      total_questions=len(room.quiz["questions"]))
        self._broadcast(room, "QUESTION_EVENT", public)

    def submit_answer(self, session, payload):
        """Validate and record once; scores stay private until close_question."""
        room = self._room(session)
        if room.state != "QUESTION_OPEN":
            raise RequestError("QUESTION_CLOSED")
        question = room.quiz["questions"][room.question_index]
        qid = self._text(payload, "question_id")
        run_id = payload.get("run_id")
        if type(run_id) is not int or run_id < 1:
            raise RequestError("INVALID_REQUEST")
        if run_id != room.run_id:
            raise RequestError("QUESTION_CLOSED")
        if qid != question["question_id"]:
            if any(q["question_id"] == qid for q in room.quiz["questions"][:room.question_index]):
                raise RequestError("QUESTION_CLOSED")
            raise RequestError("INVALID_ANSWER")
        loop = asyncio.get_running_loop()
        if loop.time() >= room.deadline:
            self.close_question(room.code, room.run_id, qid)
            raise RequestError("QUESTION_CLOSED")
        if session.session_id in room.answers:
            raise RequestError("DUPLICATE_ANSWER")
        answer = payload.get("answer")
        if not isinstance(answer, str) or answer.upper() not in ("A", "B", "C", "D"):
            raise RequestError("INVALID_ANSWER")
        room.answers[session.session_id] = answer.upper()
        self._close_if_all_answered(room)
        return {"accepted": True, "question_id": qid, "run_id": run_id,
                "number": room.question_index + 1}

    def _close_if_all_answered(self, room):
        if room.state == "QUESTION_OPEN" and len(room.answers) == len(room.players):
            room.timer.cancel()
            # Let the network layer queue the answer receipt before the result and next question.
            room.timer = asyncio.get_running_loop().call_soon(
                self.close_question, room.code, room.run_id,
                room.quiz["questions"][room.question_index]["question_id"], "all_answered")

    def close_question(self, room_code, run_id, question_id, reason="timeout"):
        """Idempotent callback: a stale room/run/question cannot close a new one."""
        room = self.rooms.get(room_code)
        if (room is None or room.state != "QUESTION_OPEN" or room.run_id != run_id
                or room.quiz["questions"][room.question_index]["question_id"] != question_id):
            return False
        if reason == "timeout":
            loop = asyncio.get_running_loop()
            if loop.time() < room.deadline:
                # A timer may fire just before its deadline; keep the timeout live.
                room.timer.cancel()
                room.timer = loop.call_at(room.deadline, self.close_question,
                                          room_code, run_id, question_id)
                return False
        room.state = "QUESTION_CLOSED"
        room.timer.cancel()
        room.timer = None
        room.deadline = None
        question = room.quiz["questions"][room.question_index]
        results = []
        for sid, player in room.players.items():
            answer = room.answers.get(sid)
            correct = answer == question["answer"]
            awarded = question["points"] if correct else 0
            player.score += awarded
            player.correct_count += int(correct)
            results.append({"user_id": player.session.user_id, "name": player.session.name,
                            "your_answer": answer, "correct": correct,
                            "points_awarded": awarded, "score": player.score})
        self._broadcast(room, "QUESTION_RESULT_EVENT", {
            "room_code": room.code, "question_id": question_id, "run_id": run_id,
            "number": room.question_index + 1, "total_questions": len(room.quiz["questions"]),
            "correct_answer": question["answer"], "points": question["points"],
            "results": results, "leaderboard": self._rank(room), "reason": reason,
        })
        room.answers.clear()
        if room.question_index + 1 < len(room.quiz["questions"]):
            self.open_question(room.code, room.question_index + 1)
        else:
            room.state = "FINISHED"
            leaderboard = self._rank(room)
            top_score = leaderboard[0]["score"]
            winners = [row["user_id"] for row in leaderboard if row["score"] == top_score]
            self._broadcast(room, "QUIZ_FINISHED_EVENT", {
                "room_code": room.code, "quiz_id": room.quiz["quiz_id"],
                "leaderboard": leaderboard, "winner_user_ids": winners,
                "winner_score": top_score, "tied": len(winners) > 1,
            })
        return True

    @staticmethod
    def _rank(room):
        players = sorted(room.players.values(), key=lambda p: (-p.score, p.session.user_id))
        rows = []
        previous_score = None
        rank = 0
        for index, player in enumerate(players, 1):
            if player.score != previous_score:
                rank = index
                previous_score = player.score
            rows.append({"rank": rank, "user_id": player.session.user_id,
                         "name": player.session.name, "score": player.score,
                         "correct_count": player.correct_count})
        return rows

    def _leaderboard(self, session, payload):
        room = self._room(session)
        return {"room_code": room.code, "state": room.state, "leaderboard": self._rank(room)}

    def _leave(self, session, payload):
        room = self._room(session)
        self.disconnect(session)
        return {"room_code": room.code}

    def disconnect(self, session):
        code = self.memberships.pop(session.session_id, None)
        if code is None:
            return
        room = self.rooms[code]
        room.players.pop(session.session_id)
        room.answers.pop(session.session_id, None)
        if not room.players:
            self._destroy(room, "last player left")
            return
        if room.host_id == session.session_id:
            if room.state == "LOBBY":
                self._destroy(room, "host left the lobby")
                return
            room.host_id = next(iter(room.players))
            host = room.players[room.host_id].session
            self._broadcast(room, "HOST_CHANGED_EVENT", {
                "room_code": room.code, "host_user_id": host.user_id, "host_name": host.name,
            })
        self._notify_players(room)
        self._close_if_all_answered(room)

    def _destroy(self, room, reason):
        self.rooms.pop(room.code)
        if room.timer is not None:
            room.timer.cancel()
        for sid in room.players:
            self.memberships.pop(sid, None)
        self._broadcast(room, "ROOM_CLOSED_EVENT", {"room_code": room.code, "reason": reason})

    def close(self):
        for room in list(self.rooms.values()):
            self._destroy(room, "server shutdown")
