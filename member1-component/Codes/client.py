"""Quiz client entry point.

    python3 client.py --server <server_IP> --port <port> --id <user_id> --name <display_name>
"""

from __future__ import annotations

import argparse
import sys

from quiznet.client import ClientError, QuizClient

PROGRAM = "client.py"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Networked quiz tournament client (command line only).",
        allow_abbrev=False,
    )
    parser.add_argument("--server", required=True, metavar="SERVER_IP", help="server address")
    parser.add_argument("--port", required=True, type=_port, metavar="PORT", help="server TCP port")
    parser.add_argument("--id", required=True, dest="user_id", metavar="USER_ID",
                        help="unique player identifier (A-Z a-z 0-9 _ -, max 32)")
    parser.add_argument("--name", metavar="DISPLAY_NAME", default=None,
                        help="display name shown to other players (defaults to the user id)")
    return parser


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer port number") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    display_name = args.name or args.user_id

    client = QuizClient(args.server, args.port, args.user_id, display_name)
    try:
        client.connect()
    except ClientError as exc:
        print(f"{PROGRAM}: {exc}", file=sys.stderr)
        return 1

    try:
        client.request("LOGIN_REQUEST", {"user_id": args.user_id, "name": display_name})
    except ClientError as exc:
        print(f"{PROGRAM}: {exc}", file=sys.stderr)
        client.close()
        return 1

    try:
        return client.run()
    except ClientError as exc:
        print(f"{PROGRAM}: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
