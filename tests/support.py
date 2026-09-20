"""Shared helpers, and the dependency tiers these tests are built around.

The import path to the code under test is set up in this package's
__init__, which runs before any test module.

Three tiers, by what a module needs to be imported at all:

1. Nothing at all - `sip_messages`, `sip_sdp`, `sip_requests`,
   `payload_types`. Plain string and number work; tests pass a `StubLine`
   instead of a real configuration.
2. Environment variables - `config`, and `sip_transport`/`sip_registrar`
   through it. `bridge_env()` supplies a complete set.
3. The media stack (numpy, av, aiortc) - `rtp`, `g711`, `g722`, `agc`,
   `sip_call`, `talk_client`, `daemon`, `control_api`. Only available in the
   deployment venv, so tests touching these are skipped elsewhere rather
   than failing.

No test in this package opens a socket or reaches the network.
"""
import contextlib
import importlib
import os
import unittest

from tests import CODE_DIR  # noqa: F401  (importing the package puts the code on the path)

# Documentation addresses (RFC 5737), so a mistake that does send something
# cannot reach a real host.
GATEWAY_HOST = "192.0.2.1"
LOCAL_IP = "198.51.100.2"
RELAY_LAN_HOST = "203.0.113.10"


class StubLine:
    """The attributes of config.LineConfig that message and SDP building
    read. A stub rather than the real thing keeps tier-1 tests free of
    environment variables."""

    def __init__(self, **overrides):
        self.id = "test"
        self.sip_user = "sip-phone"
        self.sip_pass = "secret"
        self.gateway_host = GATEWAY_HOST
        self.local_ip = LOCAL_IP
        self.local_sip_port = 5060
        self.local_rtp_port = 40000
        self.contact_host = None
        self.contact_port = None
        self.proxy_host = GATEWAY_HOST
        self.proxy_port = 5060
        self.sip_transport = "udp"
        self.contact_transport = ""   # empty: follow sip_transport
        self.media_relay_enabled = False
        self.relay_lan_host = RELAY_LAN_HOST
        self.relay_lan_port = 40000
        self.relay_overlay_host = LOCAL_IP
        self.relay_overlay_port = 40001
        self.dialin_number = ""
        self.dialout_number_allowlist = ""
        self.dialout_strip_prefix = ""
        self.dialout_internal_dial_prefix = ""
        self.default_room_token = ""
        self.notify_user = ""
        self.notify_app_password = ""
        self.__dict__.update(overrides)


def _clear_bridge_env():
    """Removes every BRIDGE_* the configuration reads. BRIDGE_CODE is not
    one of those - it says where the code under test lives, and dropping
    it would unfind the very modules being tested."""
    for key in [k for k in os.environ if k.startswith("BRIDGE_") and k != "BRIDGE_CODE"]:
        del os.environ[key]


def bridge_env(**overrides) -> dict:
    """A complete BRIDGE_* environment, for tests that need config."""
    env = {
        "BRIDGE_SIP_USER": "sip-phone",
        "BRIDGE_SIP_PASS": "secret",
        "BRIDGE_GATEWAY_HOST": GATEWAY_HOST,
        "BRIDGE_LOCAL_IP": LOCAL_IP,
        "BRIDGE_WS_URL": "ws://127.0.0.1:8080/spreed",
        "BRIDGE_INTERNAL_SECRET": "test-secret",
        "BRIDGE_BACKEND_URL": "https://nextcloud.example",
        "BRIDGE_STATE_DIR": "",
    }
    env.update(overrides)
    return env


# Several modules build their configuration as they are imported, so a test
# module cannot import them without one. This puts a complete, known
# environment in place before any of them runs - replacing whatever the
# shell carried rather than filling its gaps, because a leftover
# BRIDGE_LINES or relay address makes the same suite answer differently on
# two machines. That a module really can be imported with nothing but its
# own environment is checked separately, in test_build.py, using
# subprocesses with an environment built from scratch.
_clear_bridge_env()
os.environ.update(bridge_env())


@contextlib.contextmanager
def env(**overrides):
    """Applies bridge_env() for the duration of the block and reloads
    config, so a test never depends on how the shell was set up.

    Clearing first is what makes that true. Adding to the environment is
    not enough: a variable this function does not mention - a relay
    address, say - survives from whatever sourced a deployment's env file
    before running the tests, and the suite then answers differently
    depending on the shell it was started from."""
    previous = dict(os.environ)
    _clear_bridge_env()
    os.environ.update(bridge_env(**overrides))
    try:
        import config as config_module
        importlib.reload(config_module)
        yield config_module.config
    finally:
        os.environ.clear()
        os.environ.update(previous)
        import config as config_module
        try:
            importlib.reload(config_module)
        except RuntimeError:
            # Outside this block there need not be a usable BRIDGE_*
            # environment, and nothing outside it reads config - leaving the
            # module as the block left it is better than failing the test
            # that just passed.
            pass


def media_stack_available() -> bool:
    for name in ("numpy", "av", "aiortc"):
        try:
            importlib.import_module(name)
        except ImportError:
            return False
    return True


needs_media_stack = unittest.skipUnless(
    media_stack_available(),
    "needs the deployment venv (numpy, av, aiortc)")


def header_lines(message: bytes) -> list[str]:
    """The header block of a SIP message, as lines."""
    return message.decode().split("\r\n\r\n", 1)[0].split("\r\n")


def header_value(message: bytes, name: str) -> str:
    prefix = f"{name}: "
    for line in header_lines(message):
        if line.startswith(prefix):
            return line[len(prefix):]
    raise AssertionError(f"no {name} header in {message!r}")


def body_of(message: bytes) -> str:
    parts = message.decode().split("\r\n\r\n", 1)
    return parts[1] if len(parts) > 1 else ""
