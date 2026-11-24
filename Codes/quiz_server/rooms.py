"""Integration point for M3. All hooks execute on the server event loop."""


class RequestError(ValueError):
    """The exception text is a public protocol error code."""


class RoomService:
    def __init__(self, bank, emit):
        self.bank = bank
        self.emit = emit

    def handle(self, session, kind, payload):
        raise RequestError("ROOM_SERVICE_UNAVAILABLE")

    def disconnect(self, session):
        pass

    def close(self):
        pass
