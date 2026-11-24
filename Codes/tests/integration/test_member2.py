import asyncio
import copy
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from uuid import uuid4

from common.protocol import encode_message as encode
from quiz_server.network import Server, messages
from quiz_server.question_bank import load_and_validate_questions
from quiz_server.rooms import RoomService

CODES = Path(__file__).resolve().parents[2]
TEST_BANK = Path(__file__).resolve().parents[1] / "fixtures" / "member2_questions.json"


def request(kind, token=None, **payload):
    return dict(version=1, type=kind + "_REQUEST", request_id=uuid4().hex,
                event_id=None, session_id=token, payload=payload)


def free_port():
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RoomProbe(RoomService):
    def handle(self, session, kind, payload):
        self.emit(session, "PLAYER_STATUS_EVENT", {"members": [session.user_id]})
        return {"received": kind}

    def disconnect(self, session):
        self.last_departure = session


class Transport(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop = asyncio.get_running_loop()
        self.port = free_port()
        self.server = Server(load_and_validate_questions(TEST_BANK), RoomProbe)
        self.task = asyncio.create_task(self.server.serve("127.0.0.1", self.port))
        await asyncio.sleep(0)
        self.sockets = []
        self.readers = {}

    async def asyncTearDown(self):
        for sock in self.sockets:
            sock.close()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def connect(self):
        sock = socket.socket()
        sock.setblocking(False)
        self.sockets.append(sock)
        self.readers[sock] = messages(sock)
        await self.loop.sock_connect(sock, ("127.0.0.1", self.port))
        return sock

    async def read(self, sock):
        return await asyncio.wait_for(anext(self.readers[sock]), 3)

    async def call(self, sock, msg):
        await self.loop.sock_sendall(sock, encode(msg))
        reply = await self.read(sock)
        self.assertEqual(reply["request_id"], msg["request_id"])
        return reply


    async def test_member1_client_envelope_compatibility(self):
        """Wire responses satisfy M1's six-field envelope and login contract."""
        sock = await self.connect()
        msg = request("LOGIN", user_id="m1", name="Alice")
        reply = await self.call(sock, msg)
        self.assertEqual(set(reply), {"version", "type", "request_id", "event_id", "session_id", "payload"})
        self.assertIsNone(reply["event_id"])
        self.assertTrue(reply["payload"]["success"])
        self.assertEqual(reply["payload"]["session_id"], reply["session_id"])
        self.assertEqual(reply["payload"]["name"], "Alice")
        await self.loop.sock_sendall(sock, encode(request("JOIN_ROOM", reply["session_id"], room_code="ABCD")))
        event = await self.read(sock)
        self.assertEqual(event["type"], "PLAYER_STATUS_EVENT")
        self.assertIn("request_id", event)
        self.assertIsNone(event["request_id"])
        self.assertIsInstance(event["event_id"], str)

    async def test_ten_clients_duplicate_logout_reuse(self):
        clients = [await self.connect() for _ in range(10)]
        async def login(sock, uid):
            return await self.call(sock, request("LOGIN", user_id=uid, name="玩家"))
        same = await asyncio.gather(*(login(sock, "shared") for sock in clients))
        winner, = [i for i, r in enumerate(same) if r["type"] == "LOGIN_RESPONSE"]
        for i, reply in enumerate(same):
            if i != winner:
                self.assertEqual(reply["payload"]["code"], "DUPLICATE_USER")
                await login(clients[i], str(i))
        self.assertEqual(len(self.server.users), 10)
        await self.call(clients[winner], request("LOGOUT", same[winner]["session_id"]))
        self.assertEqual((await login(await self.connect(), "shared"))["type"], "LOGIN_RESPONSE")

    async def test_fragmentation_coalescing_and_public_catalog(self):
        sock = await self.connect()
        frame = encode(request("LOGIN", user_id="a", name="同学"))
        await self.loop.sock_sendall(sock, frame[:2])
        await asyncio.sleep(0.01)
        await self.loop.sock_sendall(sock, frame[2:])
        token = (await self.read(sock))["session_id"]
        frames = [request("LIST_QUIZZES", token), request("UNKNOWN", token)]
        await self.loop.sock_sendall(sock, b"".join(map(encode, frames)))
        catalog = await self.read(sock)
        self.assertEqual(catalog["request_id"], frames[0]["request_id"])
        for quiz in catalog["payload"]["quizzes"]:
            self.assertEqual(set(quiz), {"title", "description", "quiz_id", "question_count"})
        self.assertEqual((await self.read(sock))["payload"]["code"], "UNKNOWN_REQUEST")

    async def test_invalid_envelope_and_bad_frames_leave_other_session_live(self):
        live = await self.connect()
        token = (await self.call(live, request("LOGIN", user_id="a", name="A")))["session_id"]
        invalid = request("LIST_QUIZZES", token)
        del invalid["event_id"]
        error = await self.call(live, invalid)
        self.assertEqual(error["payload"]["code"], "INVALID_REQUEST")
        self.assertTrue(error["payload"]["message"])
        for frame in (bytes(4), (65537).to_bytes(4, "big"), b"\x00\x00\x00\x01{",
                      b"\x00\x00\x00\x01\xff"):
            bad = await self.connect()
            await self.loop.sock_sendall(bad, frame)
            with self.assertRaises(StopAsyncIteration):
                await self.read(bad)
        reply = await self.call(live, request("LIST_QUIZZES", token))
        self.assertEqual(reply["type"], "LIST_QUIZZES_RESPONSE")

    async def test_backpressure_disconnects_only_slow_peer(self):
        first, other = await self.connect(), await self.connect()
        await self.call(first, request("LOGIN", user_id="slow", name="S"))
        token = (await self.call(other, request("LOGIN", user_id="live", name="L")))["session_id"]
        old = self.server.users["slow"].session
        for i in range(64):
            self.assertTrue(self.server.emit(old, "UPDATE_EVENT", {"n": i}))
        self.assertFalse(self.server.emit(old, "UPDATE_EVENT", {}))
        async with asyncio.timeout(2):
            while "slow" in self.server.users:
                await asyncio.sleep(0.005)
        self.assertFalse(self.server.emit(old, "UPDATE_EVENT", {}))
        reply = await self.call(other, request("LIST_QUIZZES", token))
        self.assertEqual(reply["type"], "LIST_QUIZZES_RESPONSE")

    async def test_bad_frame_isolation_and_room_hook(self):
        bad, good = await self.connect(), await self.connect()
        token = (await self.call(good, request("LOGIN", user_id="a", name="A")))["session_id"]
        denied = await self.call(bad, request("JOIN_ROOM", token))
        self.assertEqual(denied["payload"]["code"], "INVALID_SESSION")
        await self.loop.sock_sendall(bad, bytes(4))
        with self.assertRaises(StopAsyncIteration):
            await self.read(bad)
        await self.loop.sock_sendall(good, encode(request("JOIN_ROOM", token, room_code="ABCD")))
        event, reply = await self.read(good), await self.read(good)
        self.assertEqual(event["type"], "PLAYER_STATUS_EVENT")
        self.assertEqual(reply["payload"]["received"], "JOIN_ROOM_REQUEST")
        good.close()
        async with asyncio.timeout(2):
            while "a" in self.server.users:
                await asyncio.sleep(0.005)
        self.assertEqual(self.server.rooms.last_departure.session_id, token)


class PairedCli(unittest.IsolatedAsyncioTestCase):
    async def test_real_clients_duplicate_catalog_logout_and_reconnect(self):
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
            await asyncio.wait_for(proc.stdout.readuntil(marker), 5)

        async def client():
            return await launch("client.py", "--server", "127.0.0.1", "--port", port,
                                "--id", "u1", "--name", "Alice")

        server = await launch("server.py", "--host", "127.0.0.1", "--port", port,
                              "--questions", "questions.json")
        await expect(server, b"SERVER_READY")
        first = await client()
        await expect(first, b"[LOGIN] connected as Alice")
        duplicate = await client()
        await expect(duplicate, b"DUPLICATE_USER")
        first.stdin.write(b"/quizzes\n")
        await expect(first, b"[QUIZZES] 2 available")
        first.stdin.write(b"/quit\n")
        await expect(first, b"[LOGOUT]")
        again = await client()
        await expect(again, b"[LOGIN] connected as Alice")
        again.kill()
        await again.wait()
        final = await client()
        await expect(final, b"[LOGIN] connected as Alice")


class Startup(unittest.TestCase):
    def args(self, path, port):
        return [sys.executable, "server.py", "--host", "127.0.0.1", "--port", str(port),
                "--questions", str(path)]

    def test_invalid_bank_before_ready(self):
        bank = load_and_validate_questions(TEST_BANK)
        bad = copy.deepcopy(bank)
        bad["quizzes"][-1]["questions"][-1]["points"] = True
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bank.json"
            for data in (None, b"{", json.dumps(bad).encode()):
                if data is not None:
                    path.write_bytes(data)
                result = subprocess.run(self.args(path, free_port()), cwd=CODES,
                                        capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(b"SERVER_READY", result.stdout)
                self.assertIn(b"startup failed", result.stderr)

    def test_cli_ready_and_login(self):
        port = free_port()
        proc = subprocess.Popen(self.args(TEST_BANK, port), cwd=CODES,
                                stdout=subprocess.PIPE)
        try:
            self.assertEqual(proc.stdout.readline().strip(), b"SERVER_READY")
            with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
                sock.sendall(encode(request("LOGIN", user_id="cli", name="Test")))
                with sock.makefile("rb") as stream:
                    size = int.from_bytes(stream.read(4), "big")
                    self.assertEqual(json.loads(stream.read(size))["type"], "LOGIN_RESPONSE")
            if os.name == "posix":
                proc.send_signal(signal.SIGINT)
                self.assertEqual(proc.wait(timeout=3), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()
