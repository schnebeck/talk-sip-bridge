<!--
talk-sip-bridge - docs/TESTING.md
How the tests are built, and how to write one that fits.

  Copyright (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
  Produced by Thorsten Schnebeck - the idea, the decisions, the testing.
  Written by Anthropic Claude Opus 5 - AI generated content.

  Free software under the GNU General Public License, version 3 or later.
  There is no warranty, to the extent permitted by law. The full text is
  in LICENSES/GPL-3.0-or-later.txt.

SPDX-FileCopyrightText: (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
SPDX-FileContributor: Anthropic Claude Opus 5 (AI generated content)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Writing a test for this bridge

Which test already covers what is in
[`tests/README.md`](../tests/README.md). This is the other half: the
machinery underneath, and what a new test has to do to fit into it.

There is no framework to learn. Everything is `unittest` from the
standard library, 509 tests in about fifteen seconds, and the only
imports a test needs are `tests.support` and the module under test:

```
python3 -m unittest discover -s tests -t .
python3 -m unittest tests.test_dtmf -v            # one file
python3 -m unittest tests.test_dtmf.DigitGuardTest.test_a_different_key_is_never_collapsed
```

## The three tiers

Everything here follows from one fact: **several modules build their
configuration while they are being imported**. `config.py` reads the
environment at import time, and `sip_transport`, `sip_registrar` and
`talk_ocs` read `config`. So what a test can import depends on what it
has arranged first, and the suite is organised in three tiers by exactly
that.

| Tier | Needs | Modules | What a test uses |
|---|---|---|---|
| 1 | nothing | `sip_messages`, `sip_sdp`, `sip_requests`, `payload_types`, `room_state`, `talk_messages`, `call`, `subscription` | `StubLine()` |
| 2 | a `BRIDGE_*` environment | `config`, `sip_transport`, `sip_registrar`, `talk_ocs` | `env()` |
| 3 | numpy, av, aiortc | `rtp`, `g711`, `g722`, `agc`, `media`, `call_media`, `sip_call`, `talk_client`, `control_api`, `daemon`, `dialin_ivr` | `@needs_media_stack` |

Tier 1 is where a test belongs whenever it can: no environment, no
skipping, and it runs on any interpreter. Tier 3 is the deployment venv
only, so those tests **skip rather than fail** where the media stack is
absent - that is what keeps the suite meaningful on a bare interpreter
and complete in the venv.

The tier list lives in two places that must agree: the docstring of
[`tests/support.py`](../tests/support.py), and `test_build.py`'s
`PURE_MODULES` / `CONFIG_MODULES` / `MEDIA_MODULES`, which import each
one **in a fresh subprocess** with nothing but the environment its tier
promises. An import that only succeeds because another test already
imported its dependency is not an import that works.

## Finding the code under test

`bridge/` is not a package and is not installed with the tests -
[`tests/__init__.py`](../tests/__init__.py) puts it on `sys.path` before
any test module runs:

```python
CODE_DIR = pathlib.Path(os.environ.get("BRIDGE_CODE")
                        or <the checkout's sibling bridge/>)
```

So `from sip_sdp import build_offer` is how a test reaches the daemon,
never `from bridge.sip_sdp import ...`. With `BRIDGE_CODE` set, the same
suite runs against an installed copy:

```
BRIDGE_CODE=/opt/talk-sip-bridge python3 -m unittest discover -s tests -t .
```

That is the mechanism that lets the tests be deleted from a deployment
without touching the daemon, and lets a checkout's tests be pointed at
what is actually running.

## What `tests/support.py` gives you

```python
from tests.support import StubLine, env, needs_media_stack, header_value, body_of
```

**`StubLine(**overrides)`** - the attributes of `config.LineConfig` that
message and SDP building read, as a plain object. A stub rather than the
real thing is what keeps tier-1 tests free of environment variables:

```python
line = StubLine(sip_transport="tcp", dialin_numbers={"**622": "4930622"})
```

**`env(**overrides)`** - a context manager that installs a complete
`BRIDGE_*` environment, reloads `config`, yields it, and puts the old
environment back. It **clears every `BRIDGE_*` first** rather than adding
to what is there. That is not tidiness: a leftover `BRIDGE_LINES` or
relay address from a sourced deployment env file makes the same suite
answer differently depending on which shell started it.

```python
with env(BRIDGE_DIALIN_NUMBERS="**622=4930622") as configured:
    self.assertEqual(configured.lines[0].dialin_numbers, {"**622": "4930622"})
```

The module-level `_clear_bridge_env()` plus `os.environ.update(bridge_env())`
at the bottom of `support.py` does the same thing once at import, so a
tier-2 module can be imported at the top of a test file at all.

**`@needs_media_stack`** - a `skipUnless` on numpy, av and aiortc being
importable. It goes on the class, not on each method.

**`header_lines()`, `header_value()`, `body_of()`** - read a SIP message
the test just built. `header_value` raises with the whole message when
the header is not there, which is the error message you want.

**The addresses are RFC 5737 documentation addresses** -
`GATEWAY_HOST = 192.0.2.1`, `LOCAL_IP = 198.51.100.2`,
`RELAY_LAN_HOST = 203.0.113.10`. Those ranges are reserved and routed
nowhere, so a bug that does open a socket cannot reach a real host.
Use them; do not invent addresses.

## Where a fake is allowed

`unittest.mock` appears in five files, and in every one of them it
replaces something at the edge of the process - never anything this
project wrote:

| Patched | In |
|---|---|
| `urllib.request.urlopen` | `test_sip_bridge_api.py` |
| `sip_call.RtpSession`, `threading.Timer` | `test_answer.py` |
| `builtins.print`, `traceback.print_exc` | `test_resilience.py` |

Everything inside the bridge's own call graph gets a **hand-written
fake** instead - `FakeTransport`, `FakeRtp`, `FakeWebSocket`,
`RecordingOpener`, `FakeCallManager`, `FakeMedia`, and a dozen more,
each a few lines long and defined in the file that needs it. They are
worth writing by hand: a fake that records what it was asked to do makes
the assertion read as the protocol it is checking, and a `MagicMock`
answers every call whether or not the method exists.

The rule this comes from: **a test must be able to fail when the code is
wrong.** Patching `talk_client.something` to check that
`talk_client.something` was called proves nothing. Patching the socket
underneath it and asserting on the bytes proves the message.

`test_api.py` is the backstop for what fakes cannot see. It reads the
source with `ast` and checks that every method one module calls on
another actually exists there - so a method reached only by a hangup or
a timeout is covered without having to reach that path, and without the
media stack.

## Recordings

`tests/fixtures/` holds real traffic from real calls, anonymised. Two
sets, one rule:

- `*.json` - signaling event streams. Session ids are replaced by the
  role they had (`human`, `bridge-internal`, `phone-virtual`), user
  names removed, chat payloads reduced to their envelope.
- `fritzbox/*.txt` and `*.wav` - whole SIP messages and keypad audio
  from the gateway this bridge is deployed against, as bytes off the
  wire. Read them with `read_bytes()`: `read_text()` translates CRLF and
  takes the message framing with it.

**They are inputs, never expected output.** What a test asserts is the
decision the bridge takes from them. A recording of our own output would
only pin today's behaviour, bugs and all - which is the difference
between a test and a snapshot.

Only what the far end originated belongs in `fritzbox/`. A capture also
holds our own replies and whatever a proxy in the path generated, and
neither of those says anything about what a gateway does. The directory
name is the disclaimer: one gateway's behaviour, not the protocol.

## Conventions

**A test name is a sentence.** `test_a_different_key_is_never_collapsed`,
`test_the_first_press_of_a_call_is_never_a_repeat`,
`test_speech_shaped_noise_is_not_a_key`. Ten words is normal here. The
name is what a failure prints, and it should say what is broken before
anybody opens the file.

**Every module opens with a docstring whose first sentence says what the
file is** - and that sentence is repeated in the licence header, where
`test_headers.py` checks the two still match. Rewriting one means
rewriting the other; that is the point of having it up there.

**A docstring on a test method says why it exists**, especially when it
came from something that went wrong:

```python
def test_a_different_key_is_never_collapsed(self):
    """The one that cost a caller their meeting id: ten digits were
    keyed in and seven arrived, because anything within the window
    was taken for a repeat whatever key it was."""
```

**`subTest` for a table**, so one failing row names itself and the rest
still run:

```python
for digit in row:
    with self.subTest(digit=digit):
        self.assertEqual(digit_in_block(dtmf(digit)[:BLOCK], RATE), digit)
```

**No test in this package opens a socket or touches the network.** If
one needs to, it belongs in `tests/hardware/`.

## Properties, where paths run out

`test_subscription_properties.py` is the one test written the other way
round. Every other test walks a path somebody thought of, which is why
they tend to be written after a call has already gone wrong. This one
throws 2000 random twelve-event sequences at the subscription state
machine and checks four promises hold in all of them:

1. A working connection is never disturbed - once audio flows, no event
   may produce an action.
2. Repairs are bounded - attempts never exceed the budget, in any order.
3. One negotiation at a time - a step from an older generation is never
   still current.
4. A closed call stays closed, and answers nothing.

It is seeded, and the seed is in the failure message, so a failing
sequence is reproducible. This shape is worth copying for anything else
that is a state machine with an invariant; it is not worth it for code
whose interesting cases can be listed.

## The two checks that read rather than run

Both need a tool from `tests/requirements.txt` and skip themselves
without it, so neither can block a deployment venv that does not carry
them.

- **`test_static.py`** runs `ruff` over `bridge/` and `tests/` with the
  rules in [`ruff.toml`](../ruff.toml). It is the only thing that sees a
  name missing *inside* a function - a module that imports perfectly and
  fails the moment that line runs. It also covers `tests/hardware/`,
  which the suite never executes, so it is what notices when a refactor
  leaves one of those scripts calling a method that moved.
- **`test_headers.py`** checks that every tracked file states a licence,
  that each header names the file it is in, and that a Python header
  still says what its module docstring says. `reuse lint` runs as part
  of it.

Nothing reformats anything. A formatter would rewrite files that are
laid out the way they read best, and in this project the comments are
half the point.

## tests/hardware/

The other kind: scripts that place real calls. They are **not
discovered** by `unittest` - the suite ignores the directory - and each
is run by hand with its own arguments. Which one needs what is the table
in [`tests/README.md`](../tests/README.md#hardware).

A script there follows a different shape from a `TestCase`:

- Its **docstring carries the command line**, including the environment
  variables and, where the setup is elaborate (a second SIP pipe, a
  second relay), the exact invocation. That docstring is the
  documentation; there is no other.
- It prints `PASS` / `FAIL` per check with the measurement beside it,
  then a `RESULT: SUCCESS` or `FAILED` line.
- `main()` returns 0 or 1 and `sys.exit(main())` carries it out, so the
  scripts chain.
- It uses **fresh ports** per run where a previous run's transport may
  still hold its own, which has nothing to do with what is being tested.

Two of them are reusable counterparts rather than tests, and they are
what lets a "hardware" test need no hardware:

- **`fake_gateway.py`** - a SIP gateway made of one UDP socket. It sends
  an INVITE addressed to whatever number the test wants and carries RTP,
  without registering. This is how dial-in can be tested at all: a
  dial-in number has to arrive as a *different* number than the one a
  person's phone rings on, which one real line cannot deliver.
- **`talk_participant.py`** - the counterpart on the Talk side: an
  internal client that joins a room and publishes a sine tone, so the
  Talk → phone direction has something real to subscribe to without a
  person holding a browser open.

Neither is a stand-in for the real thing, and both say so in their own
docstrings. They exist so a failure can be pinned to one side.

## Which tests to run for a change

The table in the [top-level `README.md`](../README.md) maps each module
to what is worth running after touching it. The short version: `tests/` for
everything, always - it costs fifteen seconds - and then whichever
hardware script covers the path that offline tests cannot reach. The
paths nothing offline covers are the two signaling connections against a
live server, and anything in the deployment or the relay host.

Before running anything against the production bridge, check that no
call is in progress, **in its own step**:

```
curl -s http://127.0.0.1:8765/status | grep active_call   # must be null
```

A check is worth nothing if it runs in the same breath as the thing that
ignores it.
