"""Room and quiz rules, with a controlled event-loop clock."""
import asyncio
import copy
import unittest
from unittest.mock import patch

from quiz_server.network import Session
from quiz_server.rooms import RequestError, RoomService


def question_bank():
    return {"schema_version": 1, "quizzes": [
        {"quiz_id": f"quiz-{n}", "title": f"Quiz {n}", "description": "Test quiz",
         "questions": [{"question_id": f"Q{n}-{i}", "text": f"Question {i}",
                        "options": {letter: letter for letter in "ABCD"},
                        "answer": "B", "points": 10 * i, "time_limit": 5}
                       for i in range(1, 6)]}
        for n in (1, 2)]}


class Handle:
    def __init__(self, when, callback, args):
        self.when, self.callback, self.args = when, callback, args
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class Clock:
    def __init__(self):
        self.now = 100.0
        self.handles = []

    def time(self):
        return self.now

    def call_at(self, when, callback, *args):
        handle = Handle(when, callback, args)
        self.handles.append(handle)
        return handle

    def call_soon(self, callback, *args):
        return self.call_at(self.now, callback, *args)

    def run_due(self):
        while True:
            due = [h for h in self.handles if not h.cancelled and h.when <= self.now]
            if not due:
                return
            handle = min(due, key=lambda h: h.when)
            handle.cancel()
            handle.callback(*handle.args)


class RoomTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.patch = patch.object(asyncio, "get_running_loop", return_value=self.clock)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.events = []
        self.service = RoomService(question_bank(), self.emit)
        self.addCleanup(self.service.close)
        self.alice = Session("alice", "Alice", "session-a")
        self.bob = Session("bob", "Bob", "session-b")
        self.charlie = Session("charlie", "Charlie", "session-c")

    def emit(self, session, kind, payload):
        self.events.append((session.session_id, kind, copy.deepcopy(payload)))
        return True

    def call(self, session, kind, **payload):
        return self.service.handle(session, kind + "_REQUEST", payload)

    def assert_error(self, code, session, kind, **payload):
        with self.assertRaises(RequestError) as caught:
            self.call(session, kind, **payload)
        self.assertEqual(str(caught.exception), code)

    def create(self, host=None, quiz_id="quiz-1"):
        return self.call(host or self.alice, "CREATE_ROOM", quiz_id=quiz_id)["room_code"]

    def start(self):
        code = self.create()
        self.call(self.bob, "JOIN_ROOM", room_code=code)
        self.call(self.alice, "READY", ready=True)
        self.call(self.bob, "READY", ready=True)
        self.call(self.alice, "START_QUIZ")
        return code

    def latest(self, kind, session=None):
        target = (session or self.alice).session_id
        return [p for sid, event, p in self.events if sid == target and event == kind][-1]

    def answer(self, player, answer="B", question_id=None):
        question = self.latest("QUESTION_EVENT", player)
        return self.call(player, "ANSWER", question_id=question_id or question["question_id"],
                         run_id=question["run_id"], answer=answer)

    def finish_question(self, first="B", second="B"):
        self.answer(self.alice, first)
        self.answer(self.bob, second)
        self.clock.run_due()

    def test_create_join_roster_and_public_listing(self):
        code = self.create()
        joined = self.call(self.bob, "JOIN_ROOM", room_code=code.lower())
        self.assertEqual(joined["room_code"], code)
        roster = self.call(self.alice, "PLAYER_LIST")
        self.assertEqual([p["user_id"] for p in roster["players"]], ["alice", "bob"])
        self.assertTrue(roster["players"][0]["is_host"])
        rooms = self.call(self.charlie, "LIST_ROOMS")["rooms"]
        self.assertEqual(rooms[0]["player_count"], 2)
        for forbidden in ("questions", "options", "answer", "text"):
            self.assertNotIn(forbidden, rooms[0])
        self.assertNotIn("session-a", str(roster))

    def test_one_session_cannot_join_or_create_two_rooms(self):
        code = self.create()
        self.assert_error("ALREADY_IN_ROOM", self.alice, "CREATE_ROOM", quiz_id="quiz-2")
        self.assert_error("ALREADY_IN_ROOM", self.alice, "JOIN_ROOM", room_code=code)

    def test_unknown_quiz_room_and_invalid_payloads(self):
        self.assert_error("QUIZ_NOT_FOUND", self.alice, "CREATE_ROOM", quiz_id="missing")
        self.assert_error("ROOM_NOT_FOUND", self.alice, "JOIN_ROOM", room_code="NONE")
        for value in (None, [], 3, ""):
            with self.subTest(value=value):
                self.assert_error("INVALID_REQUEST", self.alice, "CREATE_ROOM", quiz_id=value)
                self.assert_error("INVALID_REQUEST", self.alice, "JOIN_ROOM", room_code=value)

    def test_room_capacity(self):
        code = self.create()
        for n in range(11):
            self.call(Session(f"u{n}", f"U{n}", f"s{n}"), "JOIN_ROOM", room_code=code)
        self.assert_error("ROOM_FULL", self.bob, "JOIN_ROOM", room_code=code)

    def test_room_commands_require_membership(self):
        for kind in ("PLAYER_LIST", "READY", "START_QUIZ", "ANSWER", "LEADERBOARD", "LEAVE_ROOM"):
            with self.subTest(kind=kind):
                self.assert_error("NOT_IN_ROOM", self.alice, kind)

    def test_start_requires_two_players_host_and_readiness(self):
        code = self.create()
        self.call(self.alice, "READY", ready=True)
        self.assert_error("NOT_ENOUGH_PLAYERS", self.alice, "START_QUIZ")
        self.call(self.bob, "JOIN_ROOM", room_code=code)
        self.assert_error("NOT_ROOM_HOST", self.bob, "START_QUIZ")
        self.assert_error("PLAYERS_NOT_READY", self.alice, "START_QUIZ")
        self.call(self.bob, "READY", ready=True)
        self.assertEqual(self.call(self.alice, "START_QUIZ")["total_questions"], 5)

    def test_ready_is_boolean_and_can_be_revoked(self):
        self.create()
        self.assert_error("INVALID_REQUEST", self.alice, "READY", ready=1)
        self.call(self.alice, "READY", ready=True)
        self.call(self.alice, "READY", ready=False)
        self.assertFalse(self.call(self.alice, "PLAYER_LIST")["players"][0]["ready"])

    def test_join_start_and_ready_are_rejected_during_play(self):
        code = self.start()
        self.assert_error("GAME_IN_PROGRESS", self.charlie, "JOIN_ROOM", room_code=code)
        self.assert_error("INVALID_ROOM_STATE", self.alice, "START_QUIZ")
        self.assert_error("INVALID_ROOM_STATE", self.alice, "READY", ready=False)
        self.assertEqual(self.call(self.charlie, "LIST_ROOMS")["rooms"], [])

    def test_question_events_preserve_file_order_and_hide_answers(self):
        self.start()
        for n in range(1, 6):
            question = self.latest("QUESTION_EVENT")
            self.assertEqual(question["question_id"], f"Q1-{n}")
            self.assertEqual(question["number"], n)
            self.assertEqual(question["points"], n * 10)
            self.assertNotIn("answer", question)
            self.assertNotIn("correct_answer", question)
            self.finish_question()
        self.assertEqual(self.latest("QUIZ_FINISHED_EVENT")["winner_score"], 150)

    def test_score_is_not_exposed_before_question_closes(self):
        self.start()
        response = self.answer(self.alice)
        self.assertEqual(set(response), {"accepted", "question_id", "run_id", "number"})
        self.assertEqual(self.call(self.alice, "LEADERBOARD")["leaderboard"][0]["score"], 0)
        self.assertEqual(self.call(self.bob, "PLAYER_LIST")["players"][0]["score"], 0)
        self.answer(self.bob, "A")
        self.clock.run_due()
        self.assertEqual(self.latest("QUESTION_RESULT_EVENT")["correct_answer"], "B")
        self.assertEqual(self.call(self.alice, "LEADERBOARD")["leaderboard"][0]["score"], 10)

    def test_duplicate_and_invalid_answers_do_not_add_points(self):
        self.start()
        run_id = self.latest("QUESTION_EVENT")["run_id"]
        self.assert_error("INVALID_REQUEST", self.alice, "ANSWER", question_id="Q1-1", answer="B")
        self.assert_error("INVALID_REQUEST", self.alice, "ANSWER", question_id="Q1-1", run_id=True, answer="B")
        self.assert_error("INVALID_ANSWER", self.alice, "ANSWER", question_id="Q1-1", run_id=run_id, answer="Z")
        self.answer(self.alice, "b")
        self.assert_error("DUPLICATE_ANSWER", self.alice, "ANSWER", question_id="Q1-1", run_id=run_id, answer="B")
        self.answer(self.bob, "A")
        self.clock.run_due()
        self.assertEqual(self.latest("QUESTION_RESULT_EVENT")["leaderboard"][0]["score"], 10)

    def test_wrong_question_and_spoofed_room_are_not_used(self):
        code = self.start()
        other = self.create(self.charlie, "quiz-2")
        run_id = self.latest("QUESTION_EVENT")["run_id"]
        self.assert_error("INVALID_ANSWER", self.alice, "ANSWER", question_id="Q2-1", run_id=run_id, answer="B")
        self.call(self.alice, "ANSWER", question_id="Q1-1", run_id=run_id, answer="B",
                  room_code=other, user_id="charlie")
        self.answer(self.bob)
        self.clock.run_due()
        self.assertEqual(self.latest("QUESTION_RESULT_EVENT")["room_code"], code)
        self.assertEqual(self.call(self.charlie, "LEADERBOARD")["leaderboard"][0]["score"], 0)

    def test_deadline_boundary_rejects_even_when_timer_has_not_run(self):
        self.start()
        run_id = self.latest("QUESTION_EVENT")["run_id"]
        self.clock.now += 5
        self.assert_error("QUESTION_CLOSED", self.alice, "ANSWER", question_id="Q1-1", run_id=run_id, answer="B")
        result = self.latest("QUESTION_RESULT_EVENT")
        self.assertTrue(all(row["points_awarded"] == 0 for row in result["results"]))

    def fire_timeout_early(self, code):
        timer = self.service.rooms[code].timer
        self.clock.now = timer.when - 1e-9
        timer.cancel()  # Model a one-shot callback already consumed by the scheduler.
        timer.callback(*timer.args)
        return timer.when

    def test_early_timeout_still_closes_at_deadline(self):
        code = self.start()
        deadline = self.fire_timeout_early(code)
        self.assertEqual(self.latest("QUESTION_EVENT")["question_id"], "Q1-1")
        self.assertFalse(any(kind == "QUESTION_RESULT_EVENT" for _, kind, _ in self.events))
        self.clock.now = deadline
        self.clock.run_due()
        results = [payload for sid, kind, payload in self.events
                   if sid == self.alice.session_id and kind == "QUESTION_RESULT_EVENT"]
        self.assertEqual(len(results), 1, "The question must close once at its deadline")
        result = results[0]
        self.assertEqual(result["question_id"], "Q1-1")
        self.assertEqual(result["reason"], "timeout")
        self.assertTrue(all(row["points_awarded"] == 0 for row in result["results"]))
        self.assertEqual(self.latest("QUESTION_EVENT")["question_id"], "Q1-2")

    def test_early_timeout_followed_by_answers_does_not_close_next_question(self):
        code = self.start()
        deadline = self.fire_timeout_early(code)
        self.finish_question()
        before = len(self.events)
        self.clock.now = deadline
        self.clock.run_due()
        self.assertEqual(len(self.events), before)
        self.assertEqual(self.latest("QUESTION_EVENT")["question_id"], "Q1-2")
        self.assertEqual(self.call(self.alice, "LEADERBOARD")["leaderboard"][0]["score"], 10)

    def test_answer_just_before_deadline_is_accepted(self):
        self.start()
        self.clock.now += 4.999
        self.answer(self.alice)
        self.clock.now += .001
        self.clock.run_due()
        self.assertEqual(self.latest("QUESTION_RESULT_EVENT")["leaderboard"][0]["score"], 10)

    def test_expiry_moves_to_next_question_and_rejects_old_answer(self):
        self.start()
        run_id = self.latest("QUESTION_EVENT")["run_id"]
        self.clock.now += 5
        self.clock.run_due()
        self.assertEqual(self.latest("QUESTION_EVENT")["question_id"], "Q1-2")
        self.assert_error("QUESTION_CLOSED", self.alice, "ANSWER", question_id="Q1-1", run_id=run_id, answer="B")

    def test_all_answered_advances_after_handler_returns(self):
        self.start()
        self.answer(self.alice)
        self.answer(self.bob)
        self.assertEqual(self.latest("QUESTION_EVENT")["question_id"], "Q1-1")
        self.clock.run_due()
        self.assertEqual(self.latest("QUESTION_EVENT")["question_id"], "Q1-2")

    def test_cancelled_timeout_cannot_close_new_question(self):
        self.start()
        old_timer = self.clock.handles[-1]
        self.finish_question()
        before = len(self.events)
        old_timer.callback(*old_timer.args)
        self.assertEqual(len(self.events), before)

    def test_ranking_ties_and_finished_answers(self):
        self.start()
        for _ in range(5):
            self.finish_question()
        final = self.latest("QUIZ_FINISHED_EVENT")
        self.assertTrue(final["tied"])
        self.assertEqual(final["winner_user_ids"], ["alice", "bob"])
        self.assertEqual([p["rank"] for p in final["leaderboard"]], [1, 1])
        self.assertEqual([p["score"] for p in final["leaderboard"]], [150, 150])
        self.assert_error("QUESTION_CLOSED", self.alice, "ANSWER", question_id="Q1-5", answer="B")

    def test_rematch_resets_scores_and_ignores_previous_run_timer(self):
        self.start()
        old_timer = self.clock.handles[-1]
        for _ in range(5):
            self.finish_question()
        self.assert_error("PLAYERS_NOT_READY", self.alice, "START_QUIZ")
        self.call(self.alice, "READY", ready=True)
        self.call(self.bob, "READY", ready=True)
        self.call(self.alice, "START_QUIZ")
        before = len(self.events)
        old_timer.callback(*old_timer.args)
        self.assertEqual(len(self.events), before)
        self.assertEqual(self.call(self.alice, "LEADERBOARD")["leaderboard"][0]["score"], 0)

    def test_host_leaving_lobby_closes_room_and_clears_memberships(self):
        code = self.create()
        self.call(self.bob, "JOIN_ROOM", room_code=code)
        self.call(self.alice, "LEAVE_ROOM")
        self.assertEqual(self.latest("ROOM_CLOSED_EVENT", self.bob)["room_code"], code)
        self.assertEqual(self.call(self.bob, "LIST_ROOMS")["rooms"], [])
        self.create(self.bob)

    def test_guest_leaving_lobby_preserves_room(self):
        code = self.create()
        self.call(self.bob, "JOIN_ROOM", room_code=code)
        self.call(self.bob, "LEAVE_ROOM")
        self.assertEqual(len(self.call(self.alice, "PLAYER_LIST")["players"]), 1)

    def test_host_disconnect_during_quiz_promotes_oldest_remaining_player(self):
        self.start()
        self.service.disconnect(self.alice)
        self.assertEqual(self.latest("HOST_CHANGED_EVENT", self.bob)["host_user_id"], "bob")
        self.answer(self.bob)
        self.clock.run_due()
        self.assertEqual(self.latest("QUESTION_EVENT", self.bob)["question_id"], "Q1-2")

    def test_unanswered_player_departure_does_not_block_remaining_player(self):
        self.start()
        self.answer(self.alice)
        self.service.disconnect(self.bob)
        self.clock.run_due()
        self.assertEqual(self.latest("QUESTION_EVENT")["question_id"], "Q1-2")

    def test_empty_room_and_shutdown_cancel_pending_timers(self):
        self.start()
        self.service.disconnect(self.alice)
        self.service.disconnect(self.bob)
        before = len(self.events)
        self.clock.now += 50
        self.clock.run_due()
        self.assertEqual(len(self.events), before)
        self.assertEqual(self.call(self.charlie, "LIST_ROOMS")["rooms"], [])
        self.start()
        self.service.close()
        self.service.close()
        before = len(self.events)
        self.clock.now += 50
        self.clock.run_due()
        self.assertEqual(len(self.events), before)

    def test_two_rooms_have_separate_questions_answers_and_scores(self):
        first = self.start()
        second = self.create(self.charlie, "quiz-2")
        dave = Session("dave", "Dave", "session-d")
        self.call(dave, "JOIN_ROOM", room_code=second)
        for player in (self.charlie, dave):
            self.call(player, "READY", ready=True)
        self.call(self.charlie, "START_QUIZ")
        self.finish_question("B", "A")
        self.assertEqual(self.latest("QUESTION_EVENT", self.charlie)["question_id"], "Q2-1")
        self.assertEqual(self.call(self.charlie, "LEADERBOARD")["leaderboard"][0]["score"], 0)
        for sid, kind, payload in self.events:
            if kind in ("QUESTION_EVENT", "QUESTION_RESULT_EVENT", "QUIZ_FINISHED_EVENT"):
                self.assertEqual(payload["room_code"], first if sid in ("session-a", "session-b") else second)

    def test_stale_disconnect_does_not_remove_reconnected_identity(self):
        self.create()
        self.service.disconnect(self.alice)
        replacement = Session("alice", "Alice", "new-session-a")
        self.create(replacement)
        self.service.disconnect(self.alice)
        self.assertEqual(self.call(replacement, "PLAYER_LIST")["players"][0]["user_id"], "alice")
