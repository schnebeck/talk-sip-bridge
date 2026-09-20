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
    """Method names called on `receiver`, which may be a plain name
    ("transport") or an attribute path ("self.transport")."""
    found = set()
    for node in ast.walk(tree_of(filename)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        target = node.func.value
        if ast.unparse(target) == receiver:
            found.add(node.func.attr)
    return found


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

    def test_the_four_request_methods_are_handled(self):
        self.assertEqual(
            calls_on("sip_transport.py", "self.call_manager"),
            {"handle_invite", "handle_bye", "handle_cancel", "handle_options"})


class CallManagerToTransportTest(unittest.TestCase):
    def test_every_transport_method_used_exists(self):
        used = calls_on("sip_call.py", "self.transport")
        self.assertTrue(used)
        missing = used - methods_of("sip_transport.py", "SipTransport")
        self.assertEqual(missing, set())

    def test_registrar_only_uses_what_the_transport_offers(self):
        used = calls_on("sip_registrar.py", "transport")
        self.assertTrue(used)
        missing = used - methods_of("sip_transport.py", "SipTransport")
        self.assertEqual(missing, set())


class DaemonWiringTest(unittest.TestCase):
    """daemon.py hands CallManager's callback slots to TalkClient methods."""

    CALLBACKS = {"on_incoming_call", "on_call_connected", "on_call_ended", "on_call_failed"}

    def test_callbacks_assigned_in_the_daemon_exist_on_the_talk_client(self):
        assigned = assignments_to("daemon.py", "call_manager") & self.CALLBACKS
        self.assertEqual(assigned, self.CALLBACKS)
        missing = self.CALLBACKS - methods_of("talk_client.py", "TalkClient")
        self.assertEqual(missing, set())

    def test_call_manager_accepts_all_of_them(self):
        init_args = set()
        for node in ast.walk(tree_of("sip_call.py")):
            if isinstance(node, ast.FunctionDef) and node.name == "__init__":
                init_args = {a.arg for a in node.args.kwonlyargs}
                break
        self.assertTrue(self.CALLBACKS <= init_args, self.CALLBACKS - init_args)


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
        "build_ack": {"line", "number", "call_id", "from_tag", "branch", "cseq", "to_header"},
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

    def test_the_transport_is_named_in_exactly_one_place(self):
        """The point of the extraction: changing which transport the bridge
        speaks is a change to sip_requests, not a hunt through the call and
        registration code."""
        for filename in ("sip_call.py", "sip_registrar.py", "sip_transport.py"):
            source = (BRIDGE / filename).read_text()
            with self.subTest(file=filename):
                self.assertNotIn("SIP/2.0/UDP", source)
                self.assertNotIn("transport=tcp", source)
        requests_source = (BRIDGE / "sip_requests.py").read_text()
        self.assertIn("VIA_TRANSPORT", requests_source)
        self.assertIn("CONTACT_TRANSPORT", requests_source)


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
        for name in ("send", "wait_response", "open_waiter", "close_waiter"):
            self.assertTrue(callable(getattr(sip_transport.SipTransport, name, None)))

    @needs_media_stack
    def test_talk_client_exposes_the_callbacks_and_a_starter(self):
        with env():
            import talk_client
        for name in ("on_incoming_call", "on_call_connected", "on_call_ended", "on_call_failed"):
            self.assertTrue(callable(getattr(talk_client.TalkClient, name, None)))
        self.assertTrue(callable(talk_client.start_in_background))


if __name__ == "__main__":
    unittest.main()
