"""Scheduling, cancellation and question-close boundaries for Member 4."""
import asyncio
import socket
import unittest
from unittest.mock import Mock, patch

from quiz_server import network
from quiz_server.network import Peer, Server, Session
from tests.unit import test_rooms as fixtures


class Timing(unittest.TestCase):
    # Reuse the controlled clock and fixtures, without rerunning inherited tests.
    setUp = fixtures.RoomTests.setUp
    emit = fixtures.RoomTests.emit
    call = fixtures.RoomTests.call
    create = fixtures.RoomTests.create
    start = fixtures.RoomTests.start
    latest = fixtures.RoomTests.latest
    answer = fixtures.RoomTests.answer

    def test_timeout_and_all_answered_callbacks_settle_only_once(self):
        self.start()
        timeout = self.clock.handles[-1]
        self.clock.now += 4.999
        self.answer(self.alice)
        self.answer(self.bob)
        early_close = self.clock.handles[-1]
        self.clock.now += .001
        # Exercise either callback winning the race, then replay both.
        timeout.callback(*timeout.args)
        early_close.callback(*early_close.args)
        timeout.callback(*timeout.args)
        results = [p for sid, kind, p in self.events
                   if sid == self.alice.session_id and kind == "QUESTION_RESULT_EVENT"]
        self.assertEqual(len(results), 1)
        self.assertEqual([row["score"] for row in results[0]["leaderboard"]], [10, 10])
        self.assertEqual(self.latest("QUESTION_EVENT")["number"], 2)

    def test_queued_early_close_after_room_destroy_does_nothing(self):
        code = self.start()
        self.answer(self.alice)
        self.answer(self.bob)
        queued = self.clock.handles[-1]
        self.service.disconnect(self.alice)
        self.service.disconnect(self.bob)
        before = len(self.events)
        queued.callback(*queued.args)
        self.assertEqual(len(self.events), before)
        self.assertNotIn(code, self.service.rooms)
        self.assertEqual(self.service.memberships, {})


class Scheduling(unittest.IsolatedAsyncioTestCase):
    async def test_request_burst_yields_to_other_callbacks(self):
        server = Server(fixtures.question_bank())
        peer = Mock()
        peer.session = Session("a", "A", "token")
        replies = []
        peer.send.side_effect = lambda message: replies.append(message) or True

        async def burst(sock):
            for n in range(1000):
                yield dict(version=1, type="LIST_QUIZZES_REQUEST", request_id=str(n),
                           event_id=None, session_id="token", payload={})

        seen = []
        with patch.object(network, "messages", burst):
            reader = asyncio.create_task(server.read(peer))
            asyncio.get_running_loop().call_soon(lambda: seen.append(len(replies)))
            await reader
        self.assertEqual(len(replies), 1000)
        self.assertGreater(seen[0], 0)
        self.assertLess(seen[0], 1000, "A ready callback must run before the burst completes")

    async def test_full_queue_requests_cancellation_only_once(self):
        peer = Peer(None)
        peer.task = Mock()
        for _ in range(64):
            self.assertTrue(peer.send({"payload": {}}))
        for _ in range(3):
            self.assertFalse(peer.send({"payload": {}}))
        peer.task.cancel.assert_called_once()
        self.assertEqual(peer.out.qsize(), 64)

    async def test_socket_closes_even_if_cleanup_is_cancelled_again(self):
        server = Server(fixtures.question_bank())
        sock, other = socket.socketpair()
        self.addCleanup(sock.close)
        self.addCleanup(other.close)
        peer = Peer(sock)
        cleaning = asyncio.Event()

        async def blocked():
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await asyncio.Event().wait()

        async def read(peer):
            await blocked()

        with patch.object(server, "read", read), patch.object(peer, "write", blocked):
            task = asyncio.create_task(server.connection(peer))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.wait_for(cleaning.wait(), 1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(sock.fileno(), -1)


if __name__ == "__main__":
    unittest.main()
