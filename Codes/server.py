"""Command-line entry point scaffold; networking is not implemented yet."""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="Quiz tournament server (scaffold)")
    parser.add_argument("--host", required=True, help="Server bind address.")
    parser.add_argument("--port", required=True, type=int, help="TCP port.")
    parser.add_argument("--questions", required=True, help="Path to the question bank.")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    parser.error("Not implemented: repository scaffold only.")


if __name__ == "__main__":
    main()
