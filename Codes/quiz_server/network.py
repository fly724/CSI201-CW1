"""Raw nonblocking TCP sockets supervised on one asyncio event loop."""
import asyncio
import logging
import socket
from dataclasses import dataclass
from uuid import uuid4

from common import protocol
from quiz_server.rooms import RequestError, RoomService

ROOM_REQUESTS = {
    "CREATE_ROOM_REQUEST", "JOIN_ROOM_REQUEST", "LIST_ROOMS_REQUEST",
    "LEAVE_ROOM_REQUEST", "READY_REQUEST", "START_QUIZ_REQUEST",
    "ANSWER_REQUEST", "LEADERBOARD_REQUEST", "PLAYER_LIST_REQUEST",
}
READ_BATCH_SIZE = 16
WRITE_TIMEOUT = 5


async def messages(sock):
    """Adapt the shared incremental decoder to a nonblocking raw socket."""
    reader = protocol.FrameReader()
    loop = asyncio.get_running_loop()
    while chunk := await loop.sock_recv(sock, 8192):
        batch = []
        try:
            reader.feed(chunk, batch.append)
        except protocol.ProtocolError:
            # Deliver earlier, complete frames before closing the bad stream.
            for message in batch:
                yield message
            return
        for message in batch:
            yield message


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
        self.closing = False

    def send(self, message):
        if self.closing:
            return False
        if self.out.full():
            self.closing = True
            self.task.cancel()
            return False
        self.out.put_nowait(protocol.encode_message(message))
        return True

    async def write(self):
        loop = asyncio.get_running_loop()
        while True:
            frame = await self.out.get()
            try:
                await asyncio.wait_for(loop.sock_sendall(self.sock, frame), WRITE_TIMEOUT)
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
        return peer.send(protocol.build_event(kind, payload, uuid4().hex, session.session_id))

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
            return {"success": True, "session_id": peer.session.session_id,
                    "user_id": uid, "name": name}
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
        processed = 0
        async for request in messages(peer.sock):
            rid = request.get("request_id")
            if not valid_text(rid) or len(rid) > 64:
                rid = None
            try:
                kind, _, _ = protocol.validate_envelope(request)
                if not rid or not kind.endswith("_REQUEST") or request["event_id"] is not None:
                    raise RequestError("INVALID_REQUEST")
                payload = self.dispatch(peer, request)
                response = protocol.build_response(kind.removesuffix("_REQUEST") + "_RESPONSE",
                                                   payload, rid, peer.session.session_id)
            except (RequestError, protocol.EnvelopeError) as error:
                code = str(error) if isinstance(error, RequestError) else "INVALID_REQUEST"
                detail = code.replace("_", " ").capitalize() if isinstance(error, RequestError) else str(error)
                response = protocol.build_error(code, detail, rid,
                                                peer.session.session_id if peer.session else None)
            if not peer.send(response):
                return
            if response["type"] == "LOGOUT_RESPONSE":
                self.detach(peer)
                await peer.out.join()
                return
            processed += 1
            if processed >= READ_BATCH_SIZE:
                # Buffered requests must not monopolise the loop or starve room timers.
                processed = 0
                await asyncio.sleep(0)

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
            peer.closing = True
            reader.cancel()
            writer.cancel()
            try:
                self.detach(peer)
            finally:
                try:
                    await asyncio.gather(reader, writer, return_exceptions=True)
                finally:
                    # A second cancellation must not skip releasing the raw socket.
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
                # Stop gameplay first: disconnect cleanup must not advance a quiz on shutdown.
                self.rooms.close()
                tasks = tuple(self.tasks)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
