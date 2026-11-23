# Member 1 component - application-layer protocol and client networking

This folder contains **only** the part of CSI201 Coursework I that Member 1 owns:
the wire protocol and the command-line client. It is a self-contained working
unit: the client, the framing layer and the client tests all run without the
team server, because a small reference peer (`tools/stub_server.py`) answers
requests with canned payloads.

The full submission (server, room state machine, scoring, concurrency tests,
Docker notes and the assembled development documentation) lives in the team
archive `CSI201-CW-Part-I-TeamName.zip`. Nothing here replaces that archive.

## What Member 1 owns

| Artefact | File | Responsibility |
| --- | --- | --- |
| Frame codec and envelope validation | `quiznet/protocol.py` | 4-byte network-order length prefix, UTF-8 JSON payload, `FrameReader`, `validate_envelope`, message builders |
| Client implementation | `quiznet/client.py` | connection, request/response correlation, rendering of responses and events, command vocabulary |
| Client entry point | `client.py` | `--server --port --id --name`, exit codes |
| Protocol tests | `tests/test_protocol.py` | 22 tests: framing, limits, envelope rules, JSON edge cases |
| Client tests | `tests/test_client.py` | 19 tests: rendering, command mapping, live round trips against the stub |
| Reference peer | `tools/stub_server.py` | *not assessed*: minimal peer that speaks the contract so the client can be exercised alone |

The team interfaces Member 1 hands over are described in section 5 and section 10
of `Documentation/Member1_Protocol_and_Client.pdf`.

## Running the client

```bash
python3 client.py --server 127.0.0.1 --port 5000 --id alice --name Alice
```

Commands: `/help /quizzes /rooms /create <quiz_id> /join <room_code> /players
/ready /notready /start /answer <A|B|C|D> /score /leave /status /quit`.
A bare `A`, `B`, `C`, `D` (or `1`-`4`) is accepted while a question is open.

## Running the client against the reference peer

```bash
python3 tools/stub_server.py --port 5000        # prints STUB_READY 5000
python3 client.py --server 127.0.0.1 --port 5000 --id alice --name Alice
```

The stub answers `LOGIN`, `LIST_QUIZZES`, `LIST_ROOMS`, `CREATE_ROOM`, `JOIN_ROOM`,
`PLAYER_LIST`, `READY`, `START_QUIZ`, `ANSWER`, `LEADERBOARD`, `LEAVE_ROOM` and
`LOGOUT`, and returns `NOT_IMPLEMENTED` for anything else. It is deliberately
stateless: it does not score, enforce readiness or time questions.

## Running the tests

```bash
python3 -W ignore::ResourceWarning -m unittest discover -s tests -t .
```

Expected: `Ran 41 tests ... OK` (22 protocol + 19 client). The two live tests
start the stub on an ephemeral port, so nothing else needs to be running.

## Wire contract in one paragraph

One frame is `uint32 length` (network byte order, 1-65536) followed by exactly
that many bytes of UTF-8 JSON. Every payload is a JSON object carrying
`version` (always `1`), `type`, `request_id`, `event_id`, `session_id` and
`payload`. Requests and responses carry a `request_id`; server-initiated events
carry an `event_id` instead; exactly one of the two is non-null. `session_id`
is null until `LOGIN_RESPONSE` returns one and is echoed on every later request.
Unknown extra envelope keys are tolerated, missing or mistyped required keys are
rejected with `DecodeError`/`EnvelopeError`; a receiver must never crash on bad
input.

## Exit codes

`client.py`: `0` clean logout, `1` connection or CLI failure, `130` interrupted
with Ctrl-C. Frames that violate the contract close the connection: the client
reports `[PROTOCOL] ...` and exits rather than guessing.
