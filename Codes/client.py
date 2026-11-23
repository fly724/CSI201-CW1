"""Command-line entry point for the quiz client."""

from __future__ import annotations

import argparse
import sys

from quiz_client.client import ClientError, QuizClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quiz tournament client")
    parser.add_argument("--server", required=True, help="Server IP address.")
    parser.add_argument("--port", required=True, type=int, help="TCP port.")
    parser.add_argument("--id", required=True, help="User ID.")
    parser.add_argument("--name", required=True, help="Display name.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")

    client = QuizClient(args.server, args.port, args.id, args.name)
    try:
        client.connect()
    except ClientError as exc:
        print(f"client.py: {exc}", file=sys.stderr)
        return 1

    try:
        client.request("LOGIN_REQUEST", {"user_id": args.id, "name": args.name})
        return client.run()
    except ClientError as exc:
        print(f"client.py: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
