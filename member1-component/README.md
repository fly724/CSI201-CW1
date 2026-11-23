# CSI201 Coursework I — Member 1 component

Application-layer protocol and client networking for the Networked Quiz Tournament coursework.
This repository contains **only** Member 1's work package: the wire protocol shared by every
component, the command-line client that speaks it, the tests that hold both to the contract, and
the component record that documents the design.

## What is here

| Path | Contents |
| --- | --- |
| `Codes/quiznet/protocol.py` | Frame codec (4-byte network-order length + UTF-8 JSON), envelope validation, message builders |
| `Codes/quiznet/client.py` | Client transport, request/response correlation, rendering of responses and events, command vocabulary |
| `Codes/client.py` | Entry point: `--server --port --id --name` |
| `Codes/tools/stub_server.py` | Reference peer used by the tests — **not** the assessed server |
| `Codes/tests/` | 22 protocol tests + 19 client tests |
| `Documentation/Member1_Protocol_and_Client.pdf` | 12-page component record: framing, envelope, message catalogue, client design, verification, handover |
| `evidence/output/` | Captured test runs, client sessions and wire-level probes cited in the record |

## Requirements

CPython 3.12, standard library only. No installation step, no configuration file.

## Run the client

```bash
cd Codes
python3 client.py --server 127.0.0.1 --port 5000 --id alice --name Alice
```

Commands: `/help /quizzes /rooms /create <quiz_id> /join <room_code> /players /ready /notready
/start /answer <A|B|C|D> /score /leave /status /quit`. A bare `A`–`D` (or `1`–`4`) answers while
a question is open.

Without the team server, run the reference peer first:

```bash
python3 tools/stub_server.py --port 5000     # prints STUB_READY 5000
```

## Tests

```bash
cd Codes
python3 -W ignore::ResourceWarning -m unittest discover -s tests -t .
```

Expected: `Ran 41 tests ... OK`. The two live tests start the stub on an ephemeral port, so
nothing else needs to be running.

## Protocol in one paragraph

One frame is a 4-byte network-order length (1–65536) followed by that many bytes of UTF-8 JSON.
Every payload is one JSON object carrying `version`, `type`, `request_id`, `event_id`,
`session_id` and `payload`. Requests and responses carry a `request_id`; server-initiated events
carry an `event_id`; exactly one of the two is non-null. A framing error is terminal because the
receiver can no longer find the next message boundary; every other refusal is an
`ERROR_RESPONSE` code that leaves the session usable.

## Scope note

Room management, scoring, timing and concurrency belong to Members 2–4. The reference peer answers
requests with canned payloads so that the client can be exercised in isolation; it deliberately
enforces no quiz rules and is never presented as the server under assessment.
