"""CLI entry point for the tournament TCP server."""
import argparse
import asyncio
import sys

from quiz_server.network import Server
from quiz_server.question_bank import load_and_validate_questions


def main():
    parser = argparse.ArgumentParser(description="Quiz tournament server")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--questions", required=True)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        bank = load_and_validate_questions(args.questions)
        asyncio.run(Server(bank).serve(args.host, args.port))
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError, RecursionError) as error:
        print(f"Server startup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

