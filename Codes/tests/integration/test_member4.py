"""M4 fault isolation and shutdown using the submitted server over TCP."""
import asyncio
import os
import signal
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

from common.protocol import encode_message
from quiz_server import network
from tests.integration import test_member2 as transport
from tests.integration import test_member3 as gameplay


class Reliability(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = gameplay.Gameplay.asyncSetUp
    asyncTearDown = gameplay.Gameplay.asyncTearDown
    connect = gameplay.Gameplay.connect
    room = gameplay.Gameplay.room

    async def wait_detached(self, uid):
        peer = self.server.users[uid]
        async with asyncio.timeout(2):
            while uid in self.server.users:
                await asyncio.sleep(.005)
            # Identity removal precedes awaiting the reader/writer cleanup.
            await asyncio.gather(peer.task, return_exceptions=True)

    async def test_both_rooms_answer_concurrently_and_keep_results_separate(self):
        clients = [await self.connect(f"m4-{i}") for i in range(10)]
        groups = (clients[:5], clients[5:])
        codes = [await self.room(group, f"network_{i}")
                 for i, group in enumerate(groups, 1)]
        for number in range(1, 6):
            questions = await asyncio.gather(*(c.event("QUESTION_EVENT") for c in clients))
            replies = await asyncio.gather(*(
                c.answer(q, "B" if i < 5 else "A")
                for i, (c, q) in enumerate(zip(clients, questions))))
            self.assertTrue(all(r["type"] == "ANSWER_RESPONSE" for r in replies))
            results = await asyncio.gather(*(c.event("QUESTION_RESULT_EVENT") for c in clients))
            for i, result in enumerate(results):
                self.assertEqual(result["room_code"], codes[i // 5])
                self.assertEqual(result["number"], number)
                self.assertEqual([r["score"] for r in result["leaderboard"]],
                                 [number * 100 if i < 5 else 0] * 5)
        await asyncio.gather(*(c.event("QUIZ_FINISHED_EVENT") for c in clients))
        for i, client in enumerate(clients):
            await client.call("LIST_ROOMS")
            self.assertTrue(all(m["payload"].get("room_code", codes[i // 5]) == codes[i // 5]
                                for m in client.seen if m["type"].endswith("_EVENT")))
        self.assertEqual(len(self.server.users), 10)

    async def test_coalesced_duplicate_answers_are_accepted_once(self):
        host, guest = await self.connect("host"), await self.connect("guest")
        await self.room([host, guest], "network_1")
        question = await host.event("QUESTION_EVENT")
        requests = [transport.request("ANSWER", host.token,
                    question_id=question["question_id"], run_id=question["run_id"], answer="B")
                    for _ in range(2)]
        await asyncio.get_running_loop().sock_sendall(host.sock, b"".join(map(encode_message, requests)))
        replies = [await host.receive(), await host.receive()]
        self.assertEqual([r["request_id"] for r in replies], [r["request_id"] for r in requests])
        self.assertEqual(replies[0]["type"], "ANSWER_RESPONSE")
        self.assertEqual(replies[1]["payload"]["code"], "DUPLICATE_ANSWER")
        await guest.answer(question, "A")
        result = await host.event("QUESTION_RESULT_EVENT")
        self.assertEqual(result["leaderboard"][0]["score"], 100)
        self.assertEqual((await host.event("QUESTION_EVENT"))["number"], 2)

    async def test_write_timeout_removes_only_slow_host_and_releases_identity(self):
        host, guest, a, b = [await self.connect(uid) for uid in ("slow", "guest", "a", "b")]
        first = await self.room([host, guest], "network_1")
        second = await self.room([a, b], "network_2")
        question = await guest.event("QUESTION_EVENT")
        peer = self.server.users["slow"]
        old_session = peer.session
        loop = asyncio.get_running_loop()
        sendall = loop.sock_sendall

        async def stalled(sock, data):
            if sock is peer.sock:
                await asyncio.Event().wait()
            else:
                await sendall(sock, data)

        # Fault injection holds only the selected server writer, not other TCP traffic.
        with patch.object(loop, "sock_sendall", stalled), patch.object(network, "WRITE_TIMEOUT", .1):
            self.assertTrue(self.server.emit(old_session, "UPDATE_EVENT", {}))
            self.assertEqual((await a.call("PLAYER_LIST"))["payload"]["room_code"], second)
            await self.wait_detached("slow")
        self.assertEqual(peer.sock.fileno(), -1)
        self.assertEqual((await guest.event("HOST_CHANGED_EVENT"))["room_code"], first)
        await guest.answer(question, "B")
        self.assertEqual((await guest.event("QUESTION_RESULT_EVENT"))["leaderboard"][0]["score"], 100)
        replacement = await self.connect("slow")
        self.assertNotEqual(replacement.token, old_session.session_id)
        self.assertFalse(self.server.emit(old_session, "UPDATE_EVENT", {}))
        self.assertEqual((await b.call("PLAYER_LIST"))["payload"]["room_code"], second)

    async def test_queue_overflow_cleanup_during_play_keeps_other_room_live(self):
        host, guest, a, b = [await self.connect(uid) for uid in ("slow", "guest", "a", "b")]
        await self.room([host, guest], "network_1")
        code = await self.room([a, b], "network_2")
        peer = self.server.users["slow"]
        session = peer.session
        for _ in range(65):
            if not self.server.emit(session, "UPDATE_EVENT", {}):
                break
        else:
            self.fail("The bounded output queue must reject overflow")
        self.assertFalse(self.server.emit(session, "UPDATE_EVENT", {}))
        await self.wait_detached("slow")
        self.assertEqual(peer.sock.fileno(), -1)
        self.assertEqual((await guest.event("HOST_CHANGED_EVENT"))["host_user_id"], "guest")
        self.assertEqual((await a.call("PLAYER_LIST"))["payload"]["room_code"], code)

    async def test_query_burst_does_not_prevent_room_timeout(self):
        host, guest, flood = [await self.connect(uid) for uid in ("host", "guest", "flood")]
        code = await self.room([host, guest], "network_1")
        await host.event("QUESTION_EVENT")
        requests = b"".join(encode_message(transport.request("LIST_QUIZZES", flood.token))
                            for _ in range(300))

        async def drain_flood():
            await asyncio.get_running_loop().sock_sendall(flood.sock, requests)
            for _ in range(300):
                reply = await flood.receive()
                self.assertEqual(reply["type"], "LIST_QUIZZES_RESPONSE")

        task = asyncio.create_task(drain_flood())
        self.addAsyncCleanup(self.finish_task, task)
        self.assertEqual((await guest.call("PLAYER_LIST"))["payload"]["room_code"], code)
        result = await host.event("QUESTION_RESULT_EVENT")
        self.assertEqual(result["reason"], "timeout")
        self.assertEqual([r["points_awarded"] for r in result["results"]], [0, 0])
        await task

    @staticmethod
    async def finish_task(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_shutdown_cancels_timers_without_advancing_questions(self):
        players = [await self.connect(uid) for uid in ("a", "b", "c", "d")]
        await self.room(players[:2], "network_1")
        await self.room(players[2:], "network_2")
        # One accepted answer in each room makes disconnect-driven advancement possible.
        for client in (players[0], players[2]):
            await client.answer(await client.event("QUESTION_EVENT"), "B")
        timers = [room.timer for room in self.server.rooms.rooms.values()]
        events = []
        original = self.server.emit
        self.server.rooms.emit = lambda session, kind, payload: (
            events.append(kind), original(session, kind, payload))[1]
        peers = list(self.server.users.values())
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.assertNotIn("QUESTION_EVENT", events)
        self.assertNotIn("QUESTION_RESULT_EVENT", events)
        self.assertTrue(all(timer.cancelled() for timer in timers))
        self.assertEqual(self.server.users, {})
        self.assertEqual(self.server.rooms.rooms, {})
        self.assertEqual(self.server.rooms.memberships, {})
        self.assertTrue(all(peer.sock.fileno() == -1 for peer in peers))
        self.assertTrue(all(task.done() for task in self.server.tasks))
        # The original port can immediately be reused by the same server class.
        restarted = asyncio.create_task(self.server.serve("127.0.0.1", self.port))
        await asyncio.sleep(0)
        self.assertEqual((await (await self.connect("again")).call("LIST_QUIZZES"))["type"],
                         "LIST_QUIZZES_RESPONSE")
        restarted.cancel()
        await asyncio.gather(restarted, return_exceptions=True)


class Interrupt(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.name == "posix", "SIGINT subprocess check runs in the Linux Docker environment")
    async def test_sigint_with_two_active_rooms_releases_port(self):
        port = transport.free_port()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "server.py", "--host", "127.0.0.1", "--port", str(port),
            "--questions", str(gameplay.TEST_BANK), cwd=gameplay.CODES,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        async def cleanup():
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()

        self.addAsyncCleanup(cleanup)
        await asyncio.wait_for(proc.stdout.readuntil(b"SERVER_READY\n"), 5)
        clients = []
        for i in range(4):
            sock = socket.socket()
            sock.setblocking(False)
            self.addCleanup(sock.close)
            await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", port))
            client = gameplay.WireClient(sock)
            client.token = (await client.call("LOGIN", user_id=str(i), name=str(i)))["session_id"]
            clients.append(client)
        for i, group in enumerate((clients[:2], clients[2:]), 1):
            code = (await group[0].call("CREATE_ROOM", quiz_id=f"network_{i}"))["payload"]["room_code"]
            await group[1].call("JOIN_ROOM", room_code=code)
            for client in group:
                await client.call("READY", ready=True)
            await group[0].call("START_QUIZ")
            await group[0].answer(await group[0].event("QUESTION_EVENT"), "B")
        proc.send_signal(signal.SIGINT)
        _, error = await asyncio.wait_for(proc.communicate(), 5)
        self.assertEqual(proc.returncode, 0, error.decode())
        self.assertNotIn(b"Traceback", error)
        self.assertNotIn(b"ResourceWarning", error)
        for client in clients:
            async with asyncio.timeout(3):
                async for _ in client.reader:
                    pass
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
            listener.listen()


if __name__ == "__main__":
    unittest.main()
