"""Raw nonblocking TCP sockets supervised on one asyncio event loop."""
import asyncio
import logging
import socket
from dataclasses import dataclass
from uuid import uuid4

from common.framing import encode, read_message
from quiz_server.rooms import RequestError, RoomService

ROOM_REQUESTS = {
    "CREATE_ROOM_REQUEST", "JOIN_ROOM_REQUEST", "LIST_ROOMS_REQUEST",
    "LEAVE_ROOM_REQUEST", "READY_REQUEST", "START_QUIZ_REQUEST",
    "ANSWER_REQUEST", "LEADERBOARD_REQUEST", "PLAYER_LIST_REQUEST",
}


@dataclass(frozen=True)
class Session:
    user_id: str
    name: str
    session_id: str


def valid_text(value):
    return isinstance(value, str) and 0 < len(value) <= 128 and value.isprintable() and value.strip()


class Peer:
    def __init__(self, sock):
        self.sock = sock
        self.session = None
        self.out = asyncio.Queue(maxsize=64)
        self.task = None

    def send(self, message):
        if self.out.full():
            self.task.cancel()
            return False
        self.out.put_nowait(encode(message))
        return True

    async def write(self):
        loop = asyncio.get_running_loop()
        while True:
            frame = await self.out.get()
            try:
                await asyncio.wait_for(loop.sock_sendall(self.sock, frame), 5)
            finally:
                self.out.task_done()


class Server:
    def __init__(self, bank, room_factory=RoomService):
        self.users = {}
        self.tasks = set()
        self.rooms = room_factory(bank, self.emit)
        self.catalog = [
            {key: quiz[key] for key in ("quiz_id", "title", "description")}
            | {"question_count": len(quiz["questions"])} for quiz in bank["quizzes"]
        ]

    def emit(self, session, kind, payload):
        peer = self.users.get(session.user_id)
        if peer is None or peer.session != session:
            return False
        return peer.send(dict(version=1, type=kind, session_id=session.session_id,
                              event_id=uuid4().hex, payload=payload))

    def dispatch(self, peer, request):
        kind = request["type"]
        payload = request["payload"]
        token = request.get("session_id")
        if kind == "LOGIN_REQUEST":
            if peer.session or token is not None:
                raise RequestError("INVALID_SESSION")
            uid, name = payload.get("user_id"), payload.get("name")
            if not valid_text(uid) or not valid_text(name):
                raise RequestError("INVALID_IDENTITY")
            if uid in self.users:
                raise RequestError("DUPLICATE_USER")
            peer.session = Session(uid, name, uuid4().hex)
            self.users[uid] = peer
            return {"user_id": uid, "name": name}
        if not peer.session or token != peer.session.session_id:
            raise RequestError("INVALID_SESSION")
        if kind == "LOGOUT_REQUEST":
            return {}
        if kind == "LIST_QUIZZES_REQUEST":
            return {"quizzes": self.catalog}
        if kind in ROOM_REQUESTS:
            return self.rooms.handle(peer.session, kind, payload)
        raise RequestError("UNKNOWN_REQUEST")

    async def read(self, peer):
        loop = asyncio.get_running_loop()
        while True:
            request = await read_message(loop, peer.sock)
            rid = request.get("request_id") if isinstance(request, dict) else None
            try:
                if not (valid_text(rid) and type(request.get("version")) is int
                        and request["version"] == 1 and valid_text(request.get("type"))
                        and isinstance(request.get("payload"), dict)):
                    raise RequestError("INVALID_REQUEST")
                payload = self.dispatch(peer, request)
                kind = request["type"].removesuffix("_REQUEST") + "_RESPONSE"
            except RequestError as error:
                kind, payload = "ERROR_RESPONSE", {"code": str(error)}
            if not peer.send(dict(version=1, type=kind, request_id=rid if valid_text(rid) else None,
                                  session_id=peer.session.session_id if peer.session else None,
                                  payload=payload)):
                return
            if kind == "LOGOUT_RESPONSE":
                self.detach(peer)
                await peer.out.join()
                return

    def detach(self, peer):
        session = peer.session
        if session:
            peer.session = None
            del self.users[session.user_id]
            self.rooms.disconnect(session)

    async def connection(self, peer):
        reader = asyncio.create_task(self.read(peer))
        writer = asyncio.create_task(peer.write())
        try:
            done, _ = await asyncio.wait((reader, writer), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (EOFError, OSError, ValueError, RecursionError):
            pass
        except Exception:
            logging.exception("Connection handler failed")
        finally:
            reader.cancel()
            writer.cancel()
            try:
                self.detach(peer)
            finally:
                await asyncio.gather(reader, writer, return_exceptions=True)
                peer.sock.close()

    async def serve(self, host, port):
        loop = asyncio.get_running_loop()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.setblocking(False)
            listener.bind((host, port))
            listener.listen(128)
            print("SERVER_READY", flush=True)
            try:
                while True:
                    sock, _ = await loop.sock_accept(listener)
                    sock.setblocking(False)
                    peer = Peer(sock)
                    peer.task = asyncio.create_task(self.connection(peer))
                    self.tasks.add(peer.task)
                    peer.task.add_done_callback(self.tasks.discard)
            finally:
                listener.close()
                for task in self.tasks:
                    task.cancel()
                await asyncio.gather(*self.tasks, return_exceptions=True)
                self.rooms.close()

