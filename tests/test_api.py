"""Do the parts still fit together: the calls one module makes into
another must exist there.

These contracts are checked by reading the source rather than by importing
it, so they hold even where the media stack is not installed - and so that
a method called on only one code path (a hangup, a CANCEL) is covered
without having to reach that path.
"""
import ast
import inspect
import pathlib
import unittest

from tests import CODE_DIR
from tests.support import env, needs_media_stack

import sip_requests

BRIDGE = CODE_DIR


def tree_of(filename: str) -> ast.AST:
    return ast.parse((BRIDGE / filename).read_text())


def methods_of(filename: str, class_name: str) -> set:
    for node in ast.walk(tree_of(filename)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    raise AssertionError(f"no class {class_name} in {filename}")


def calls_on(filename: str, receiver: str) -> set:
    """Everything reached through `receiver` - called, or referenced to be
    called later. A dispatch table holds the second kind, and a contract
    test that only sees the first would miss a whole handler set."""
    found = set()
    for node in ast.walk(tree_of(filename)):
        if isinstance(node, ast.Attribute) and ast.unparse(node.value) == receiver:
            found.add(node.attr)
    return found


def methods_of_any(filename: str, *class_names: str) -> set:
    """The interface several classes present together - a base and its
    implementations."""
    found = set()
    for name in class_names:
        found |= methods_of(filename, name)
    return found


TRANSPORTS = ("_SipTransportBase", "UdpSipTransport", "TcpSipTransport")


def assignments_to(filename: str, receiver: str) -> set:
    found = set()
    for node in ast.walk(tree_of(filename)):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and ast.unparse(target).startswith(receiver + "."):
                    found.add(target.attr)
    return found


class TransportToCallManagerTest(unittest.TestCase):
    """SipTransport turns an incoming request into a CallManager call."""

    def test_every_dispatched_handler_exists(self):
        dispatched = calls_on("sip_transport.py", "self.call_manager")
        self.assertTrue(dispatched, "transport dispatches nothing - contract test is broken")
        missing = dispatched - methods_of("sip_call.py", "CallManager")
        self.assertEqual(missing, set())

    def test_every_request_method_a_gateway_sends_in_a_call_is_handled(self):
        """Including INFO: an in-dialog request nobody answers is
        retransmitted and then taken as a dead dialog."""
        self.assertEqual(
            calls_on("sip_transport.py", "self.call_manager"),
            {"handle_invite", "handle_bye", "handle_cancel", "handle_options", "handle_info"})


class CallManagerToTransportTest(unittest.TestCase):
    def test_every_transport_method_used_exists(self):
        used = calls_on("sip_call.py", "self.transport")
        self.assertTrue(used)
        missing = used - methods_of_any("sip_transport.py", *TRANSPORTS)
        self.assertEqual(missing, set())

    def test_registrar_only_uses_what_the_transport_offers(self):
        used = calls_on("sip_registrar.py", "transport")
        self.assertTrue(used)
        missing = used - methods_of_any("sip_transport.py", *TRANSPORTS)
        self.assertEqual(missing, set())

    def test_both_transports_present_the_same_interface(self):
        """A line picks one of them from configuration; anything only one of
        them has would work until somebody switches transport."""
        shared = methods_of("sip_transport.py", "_SipTransportBase")
        udp = methods_of("sip_transport.py", "UdpSipTransport") | shared
        tcp = methods_of("sip_transport.py", "TcpSipTransport") | shared
        public = lambda names: {n for n in names if not n.startswith("_")}
        self.assertEqual(public(udp), public(tcp))


class DaemonWiringTest(unittest.TestCase):
    """daemon.py hands CallManager's callback slots to TalkClient methods."""

    CALLBACKS = {"on_incoming_call", "on_call_connected", "on_call_ended", "on_call_failed"}

    def test_callbacks_assigned_in_the_daemon_exist_on_the_talk_client(self):
        assigned = assignments_to("daemon.py", "call_manager") & self.CALLBACKS
        self.assertEqual(assigned, self.CALLBACKS)
        missing = self.CALLBACKS - methods_of("talk_client.py", "TalkClient")
        self.assertEqual(missing, set())

    def test_call_manager_accepts_all_of_them(self):
        self.assertTrue(self.CALLBACKS <= keyword_arguments_of("sip_call.py", "CallManager", "__init__"),
                        self.CALLBACKS - keyword_arguments_of("sip_call.py", "CallManager", "__init__"))


class ControlApiTest(unittest.TestCase):
    def test_registrar_and_call_manager_methods_used_by_the_api_exist(self):
        registrar_methods = methods_of("sip_registrar.py", "SipRegistrar")
        manager_methods = methods_of("sip_call.py", "CallManager")
        for name in ("turn_on", "turn_off", "status", "was_registered_before_restart"):
            self.assertIn(name, registrar_methods)
        for name in ("status", "hangup", "dial"):
            self.assertIn(name, manager_methods)


class MessageBuilderApiTest(unittest.TestCase):
    """The builders are the single place that knows the wire format, so
    their shape is part of the module boundary."""

    EXPECTED = {
        "build_register": {"line", "call_id", "tag", "branch", "cseq", "expires",
                           "auth_header", "wildcard_contact"},
        "build_invite": {"line", "number", "call_id", "from_tag", "branch", "cseq",
                         "sdp", "auth_header"},
        "build_ack": {"line", "number", "call_id", "from_tag", "branch", "cseq", "to_header",
                      "request_uri"},
        "build_cancel": {"line", "number", "call_id", "from_tag", "branch", "cseq"},
        "build_bye": {"line", "request_uri", "call_id", "from_header", "to_header",
                      "branch", "cseq"},
        "build_response": {"status_line", "req_headers", "extra_headers", "body", "to_tag"},
    }

    def test_builders_exist_with_the_expected_parameters(self):
        for name, expected in self.EXPECTED.items():
            with self.subTest(builder=name):
                func = getattr(sip_requests, name, None)
                self.assertIsNotNone(func, f"sip_requests.{name} is gone")
                actual = set(inspect.signature(func).parameters)
                self.assertEqual(actual, expected)

    def test_no_module_hardcodes_a_transport_into_a_message(self):
        """Which transport a line speaks is configuration. A literal in the
        call or registration code would apply it to every deployment,
        which is how Contact came to claim TCP on a UDP socket."""
        for filename in ("sip_call.py", "sip_registrar.py", "sip_transport.py", "sip_requests.py"):
            source = (BRIDGE / filename).read_text()
            with self.subTest(file=filename):
                self.assertNotIn("SIP/2.0/UDP", source)
                self.assertNotIn("SIP/2.0/TCP", source)
                self.assertNotIn("transport=tcp", source)
                self.assertNotIn("transport=udp", source)

    def test_the_transport_is_derived_in_exactly_one_place(self):
        self.assertTrue(callable(sip_requests.via_transport))
        self.assertTrue(callable(sip_requests.contact_transport))
        source = (BRIDGE / "sip_requests.py").read_text()
        self.assertEqual(source.count("line.sip_transport"), 2, "derived somewhere else too")


def keyword_arguments_of(filename: str, class_name: str, method: str) -> set:
    """The keyword-only arguments of one method of one class. Scoped to the
    class on purpose: "the first __init__ in the file" stops meaning what
    it looks like the moment a second class appears above it."""
    for node in ast.walk(tree_of(filename)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method:
                    return {a.arg for a in item.args.kwonlyargs}
    raise AssertionError(f"no {class_name}.{method} in {filename}")


def assigned_self_attributes(filename: str, *, inside: str = None) -> set:
    """Every `self.x = ...` in a file, optionally only within one class."""
    tree = tree_of(filename)
    scopes = [tree]
    if inside:
        scopes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == inside]
    found = set()
    for scope in scopes:
        for node in ast.walk(scope):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                            and target.value.id == "self"):
                        found.add(target.attr)
    return found


class HardwareScriptTest(unittest.TestCase):
    """The scripts in tests/hardware find the code the same way this
    package does - through BRIDGE_CODE. One that hardcodes an install path
    instead silently tests whatever is deployed there, whatever it was
    pointed at."""

    def scripts(self):
        return sorted((pathlib.Path(__file__).resolve().parent / "hardware").glob("*.py"))

    def test_none_of_them_hardcodes_an_installation_path(self):
        for script in self.scripts():
            with self.subTest(script=script.name):
                self.assertNotIn("/opt/talk-sip-bridge", script.read_text())

    def test_each_of_them_honours_bridge_code(self):
        """Whoever loads the bridge's modules has to be told which copy.
        A script that loads none - a probe that speaks nothing but UDP,
        and runs on a host where the bridge is not installed - has
        nothing to point at, and saying so is the condition here: put a
        path on sys.path, and it comes from BRIDGE_CODE."""
        for script in self.scripts():
            source = script.read_text()
            if "sys.path.insert" not in source:
                continue
            with self.subTest(script=script.name):
                self.assertIn("BRIDGE_CODE", source)


class SubscriptionWiringTest(unittest.TestCase):
    """The negotiation's decisions live in subscription.py and its doing
    in human_audio.py. A repair that acts without asking the machine is
    how two of them ran at once."""

    def test_the_client_asks_the_machine_before_repairing(self):
        source = (BRIDGE / "human_audio.py").read_text()
        for decision in ("state.start()", "state.offer(", "state.refused()",
                         "state.no_publisher()", "state.media_arrived()"):
            with self.subTest(decision=decision):
                self.assertIn(decision, source)

    def test_no_second_retry_loop_survives_beside_it(self):
        """The old loop counted its own attempts; two counters mean two
        budgets and neither knows when to stop."""
        source = (BRIDGE / "human_audio.py").read_text()
        self.assertNotIn("for attempt in range(MAX_ATTEMPTS)", source)
        self.assertNotIn("subscribe_retries", source)

    def test_the_machine_needs_nothing_from_the_bridge(self):
        """It is pure so it can be tested without a call: no sockets, no
        signaling, no media stack."""
        source = (BRIDGE / "subscription.py").read_text()
        for forbidden in ("import asyncio", "import socket", "websockets",
                          "talk_messages", "numpy"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


class StubLineTest(unittest.TestCase):
    """tests/support.py's StubLine stands in for a LineConfig. A field
    added to the real one and not to the stub fails only in whichever test
    happens to read it - which is how a transport field went missing from
    two hardware scripts and a dialout field from the stub on the same
    day."""

    def test_the_stub_carries_every_field_a_line_has(self):
        real = assigned_self_attributes("config.py", inside="LineConfig")
        stub = set()
        support = pathlib.Path(__file__).resolve().parent / "support.py"
        for node in ast.walk(ast.parse(support.read_text())):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                            and target.value.id == "self"):
                        stub.add(target.attr)
        self.assertEqual(real - stub, set(), "StubLine is missing fields LineConfig sets")


class ImportableApiTest(unittest.TestCase):
    """The same contracts, confirmed against the real objects where the
    media stack allows them to be imported.

    These modules build their configuration as they are imported, so the
    import happens inside env() rather than at the top of this file."""

    @needs_media_stack
    def test_call_manager_and_transport_agree_at_runtime(self):
        with env():
            import sip_call
            import sip_transport
        for name in ("handle_invite", "handle_bye", "handle_cancel", "handle_options"):
            self.assertTrue(callable(getattr(sip_call.CallManager, name, None)))
        for transport in (sip_transport.UdpSipTransport, sip_transport.TcpSipTransport):
            for name in ("send", "wait_response", "open_waiter", "close_waiter", "close"):
                self.assertTrue(callable(getattr(transport, name, None)), f"{transport.__name__}.{name}")

    @needs_media_stack
    def test_talk_client_exposes_the_callbacks_and_a_starter(self):
        with env():
            import talk_client
        for name in ("on_incoming_call", "on_call_connected", "on_call_ended", "on_call_failed"):
            self.assertTrue(callable(getattr(talk_client.TalkClient, name, None)))
        self.assertTrue(callable(talk_client.start_in_background))


if __name__ == "__main__":
    unittest.main()
