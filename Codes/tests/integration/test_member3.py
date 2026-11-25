"""Paired M1/M2/M3 flows over real TCP sockets and CLI processes."""
import asyncio
from contextlib import closing
from pathlib import Path
import re
import socket
import subprocess
import sys
import unittest
from uuid import uuid4

from common.protocol import encode_message
from quiz_server.network import Server, messages
from quiz_server.question_bank import load_and_validate_questions

CODES = Path(__file__).resolve().parents[2]
TEST_BANK = Path(__file__).resolve().parents[1] / "fixtures" / "member2_questions.json"


def free_port():
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class WireClient:
    def __init__(self, sock):
        self.sock = sock
        self.reader = messages(sock)
        self.token = None
        self.pending = []
        self.seen = []

    async def receive(self):
        message = await asyncio.wait_for(anext(self.reader), 8)
        self.seen.append(message)
        return message

    async def call(self, kind, **payload):
        rid = uuid4().hex
        message = dict(version=1, type=kind + "_REQUEST", request_id=rid,
                       event_id=None, session_id=self.token, payload=payload)
        await asyncio.get_running_loop().sock_sendall(self.sock, encode_message(message))
        while True:
            message = await self.receive()
            if message["request_id"] == rid:
                return message
            self.pending.append(message)

    async def answer(self, question, letter, **fields):
        return await self.call("ANSWER", question_id=question["question_id"],
                               run_id=question["run_id"], answer=letter, **fields)

    async def event(self, kind):
        for index, message in enumerate(self.pending):
            if message["type"] == kind:
                return self.pending.pop(index)["payload"]
        while True:
            message = await self.receive()
            if message["type"] == kind:
                return message["payload"]
            self.pending.append(message)


class Gameplay(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bank = load_and_validate_questions(TEST_BANK)
        for quiz in bank["quizzes"]:
            for question in quiz["questions"]:
                question["time_limit"] = 5
        self.server = Server(bank)
        self.port = free_port()
        self.task = asyncio.create_task(self.server.serve("127.0.0.1", self.port))
        await asyncio.sleep(0)
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            client.sock.close()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def connect(self, uid):
        sock = socket.socket()
        sock.setblocking(False)
        client = WireClient(sock)
        self.clients.append(client)
        await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", self.port))
        reply = await client.call("LOGIN", user_id=uid, name=uid)
        self.assertEqual(reply["type"], "LOGIN_RESPONSE")
        client.token = reply["session_id"]
        return client

    async def room(self, players, quiz_id):
        reply = await players[0].call("CREATE_ROOM", quiz_id=quiz_id)
        self.assertEqual(reply["type"], "CREATE_ROOM_RESPONSE")
        code = reply["payload"]["room_code"]
        for player in players[1:]:
            reply = await player.call("JOIN_ROOM", room_code=code)
            self.assertEqual(reply["type"], "JOIN_ROOM_RESPONSE")
        for player in players:
            await player.call("READY", ready=True)
        await players[0].call("START_QUIZ")
        return code

    async def test_ten_clients_complete_two_isolated_quizzes(self):
        clients = [await self.connect(f"u{i:02}") for i in range(10)]
        groups = (clients[:5], clients[5:])
        codes = [await self.room(group, f"network_{i}")
                 for i, group in enumerate(groups, 1)]
        for number in range(1, 6):
            for room_index, group in enumerate(groups, 1):
                questions = await asyncio.gather(*(c.event("QUESTION_EVENT") for c in group))
                for question in questions:
                    self.assertEqual(question["question_id"], f"NET{room_index}_{number:03}")
                    self.assertEqual(question["room_code"], codes[room_index - 1])
                    self.assertNotIn("answer", question)
                replies = await asyncio.gather(*(
                    c.answer(questions[i], "B" if i == 0 else "A")
                    for i, c in enumerate(group)))
                self.assertTrue(all(r["type"] == "ANSWER_RESPONSE" for r in replies))
                results = await asyncio.gather(*(c.event("QUESTION_RESULT_EVENT") for c in group))
                for result in results:
                    self.assertEqual(result["leaderboard"][0]["score"], 100 * number)
                    self.assertEqual(result["correct_answer"], "B")
                    self.assertEqual(len(result["results"]), 5)
        for group, code in zip(groups, codes):
            for client in group:
                finished = await client.event("QUIZ_FINISHED_EVENT")
                self.assertEqual(finished["winner_user_ids"],
                                 ["u00" if group is groups[0] else "u05"])
                self.assertEqual([r["score"] for r in finished["leaderboard"]], [500, 0, 0, 0, 0])
                await client.call("LIST_ROOMS")  # Flush all earlier pushed events.
                self.assertTrue(all(m["payload"].get("room_code", code) == code
                                    for m in client.seen if m["type"].endswith("_EVENT")))
        self.assertEqual(len(self.server.users), 10)

    async def test_invalid_answers_host_disconnect_and_bad_peer_are_isolated(self):
        host, guest, other_host, other_guest = [await self.connect(uid) for uid in ("a", "b", "c", "d")]
        first = await self.room([host, guest], "network_1")
        other = await self.room([other_host, other_guest], "network_2")
        question = await host.event("QUESTION_EVENT")
        invalid = await host.answer(question, "E")
        self.assertEqual(invalid["payload"]["code"], "INVALID_ANSWER")
        await host.answer(question, "B")
        duplicate = await host.answer(question, "A")
        self.assertEqual(duplicate["payload"]["code"], "DUPLICATE_ANSWER")
        score = await guest.call("LEADERBOARD")
        self.assertEqual([r["score"] for r in score["payload"]["leaderboard"]], [0, 0])
        host.sock.close()
        changed = await guest.event("HOST_CHANGED_EVENT")
        self.assertEqual(changed["host_user_id"], "b")
        self.assertEqual(changed["room_code"], first)
        await guest.answer(question, "B")
        result = await guest.event("QUESTION_RESULT_EVENT")
        self.assertEqual([r["user_id"] for r in result["leaderboard"]], ["b"])
        self.assertEqual(result["leaderboard"][0]["score"], 100)
        bad = await self.connect("bad")
        await asyncio.get_running_loop().sock_sendall(bad.sock, bytes(4))
        with self.assertRaises(StopAsyncIteration):
            await bad.receive()
        untouched = await other_host.call("PLAYER_LIST")
        self.assertEqual(untouched["payload"]["room_code"], other)
        self.assertEqual(len(untouched["payload"]["players"]), 2)
        other_question = await other_guest.event("QUESTION_EVENT")
        self.assertEqual(other_question["number"], 1)
        wrong_room = await guest.answer(other_question, "B", room_code=other)
        self.assertEqual(wrong_room["payload"]["code"], "QUESTION_CLOSED")

    async def test_real_timeout_does_not_block_another_room(self):
        host, guest, fast_host, fast_guest = [await self.connect(uid) for uid in ("a", "b", "c", "d")]
        await self.room([host, guest], "network_1")
        started = asyncio.get_running_loop().time()
        first = await host.event("QUESTION_EVENT")
        await host.answer(first, "B")
        await self.room([fast_host, fast_guest], "network_2")
        for _ in range(5):
            question = await fast_host.event("QUESTION_EVENT")
            for client in (fast_host, fast_guest):
                await client.answer(question, "B")
        finished = await fast_host.event("QUIZ_FINISHED_EVENT")
        self.assertTrue(finished["tied"])
        result = await host.event("QUESTION_RESULT_EVENT")
        self.assertGreaterEqual(asyncio.get_running_loop().time() - started, 4.8)
        self.assertEqual(result["reason"], "timeout")
        self.assertEqual([r["points_awarded"] for r in result["results"]], [100, 0])
        self.assertEqual((await host.event("QUESTION_EVENT"))["number"], 2)
        late = await guest.answer(first, "B")
        self.assertEqual(late["payload"]["code"], "QUESTION_CLOSED")
        score = await fast_guest.call("LEADERBOARD")
        self.assertEqual(score["payload"]["state"], "FINISHED")
        self.assertEqual([r["score"] for r in score["payload"]["leaderboard"]], [500, 500])

    async def test_previous_run_answer_does_not_count_in_rematch(self):
        host, guest = await self.connect("a"), await self.connect("b")
        await self.room([host, guest], "network_1")
        first = await host.event("QUESTION_EVENT")
        question = first
        for number in range(1, 6):
            if number > 1:
                question = await host.event("QUESTION_EVENT")
            for player in (host, guest):
                reply = await player.answer(question, "B")
                self.assertEqual(reply["type"], "ANSWER_RESPONSE")
            await host.event("QUESTION_RESULT_EVENT")
        await host.event("QUIZ_FINISHED_EVENT")

        for player in (host, guest):
            await player.call("READY", ready=True)
        await host.call("START_QUIZ")
        first_again = await host.event("QUESTION_EVENT")
        self.assertEqual(first_again["question_id"], first["question_id"])
        self.assertNotEqual(first_again["run_id"], first["run_id"])

        stale = await host.answer(first, "B")
        self.assertEqual(stale["payload"]["code"], "QUESTION_CLOSED")
        current = await host.answer(first_again, "A")
        self.assertEqual(current["type"], "ANSWER_RESPONSE")
        self.assertEqual(current["payload"]["run_id"], first_again["run_id"])
        await guest.answer(first_again, "B")
        result = await host.event("QUESTION_RESULT_EVENT")
        self.assertEqual(result["run_id"], first_again["run_id"])
        self.assertEqual(result["results"][0]["your_answer"], "A")
        self.assertEqual(result["results"][0]["points_awarded"], 0)

    async def test_lobby_host_logout_closes_room_but_not_guest_session(self):
        host, guest = await self.connect("a"), await self.connect("b")
        created = await host.call("CREATE_ROOM", quiz_id="network_1")
        await guest.call("JOIN_ROOM", room_code=created["payload"]["room_code"])
        await host.call("LOGOUT")
        self.assertIn("host left", (await guest.event("ROOM_CLOSED_EVENT"))["reason"])
        reply = await guest.call("CREATE_ROOM", quiz_id="network_2")
        self.assertEqual(reply["type"], "CREATE_ROOM_RESPONSE")
        again = await self.connect("a")
        self.assertNotEqual(again.token, host.token)


class PairedGameplayCli(unittest.IsolatedAsyncioTestCase):
    async def test_existing_m1_cli_can_answer_all_five_questions(self):
        port = str(free_port())

        async def launch(script, *args):
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", script, *args, cwd=CODES,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            async def cleanup():
                if proc.returncode is None:
                    proc.kill()
                await proc.communicate()

            self.addAsyncCleanup(cleanup)
            return proc

        async def expect(proc, marker):
            return await asyncio.wait_for(proc.stdout.readuntil(marker), 8)

        async def command(proc, line):
            proc.stdin.write(line.encode() + b"\n")
            await proc.stdin.drain()

        server = await launch("server.py", "--host", "127.0.0.1", "--port", port,
                              "--questions", str(TEST_BANK))
        await expect(server, b"SERVER_READY\n")
        host, guest = [await launch("client.py", "--server", "127.0.0.1", "--port", port,
                                   "--id", uid, "--name", uid) for uid in ("Alice", "Bob")]
        for client in (host, guest):
            await expect(client, b"Type /help for the command list.\n")
        await command(host, "/create network_1")
        created = await expect(host, b"for quiz network_1\n")
        code = re.search(rb"\[ROOM\] created (\w+) for quiz", created).group(1).decode()
        await command(guest, f"/join {code}")
        await expect(guest, f"[ROOM] joined {code} (Networking 1)\n".encode())
        for client in (host, guest):
            await command(client, "/ready")
            await expect(client, b"[READY] you are now ready\n")
        await command(host, "/start")
        for number in range(1, 6):
            for client in (host, guest):
                output = await expect(client, b"within 15s\n")
                self.assertIn(f"[QUESTION {number}/5] NET1_{number:03}".encode(), output)
                self.assertNotIn(b"[ERROR]", output)
                await command(client, "/status")
                await expect(client, b"question_open=True\n")
            await command(host, "/answer B")
            await command(guest, "/answer A")
        for client in (host, guest):
            output = await expect(client, b"QUIZ FINISHED: winner Alice with 500 points\n")
            self.assertIn(b"[RESULT 5/5]", output)
            self.assertNotIn(b"[ERROR]", output)
            await command(client, "/quit")
            await expect(client, b"[LOGOUT] session closed\n")
            self.assertEqual(await asyncio.wait_for(client.wait(), 3), 0)
