"""Registration of one line at its gateway, kept alive for as long as the
line is meant to be on.

Whether a line should be registered survives a restart: the desired state is
written to a marker file, so a crash-triggered restart resumes instead of
leaving the line silently deregistered.
"""
import os
import secrets
import threading
import time

import sip_requests
from config import config
from sip_messages import digest_response, parse_auth_challenge, parse_sip_headers


class SipRegistrar:
    def __init__(self, transport_holder, line):
        self.line = line
        self.lock = threading.Lock()
        self.registered = False
        self.keepalive_thread = None
        self.stop_event = threading.Event()
        self.last_error = None
        self._transport_holder = transport_holder
        # Whether registration should be on is otherwise only ever held in
        # memory - a crash (or any process restart, e.g. systemd's
        # Restart=on-failure) would silently leave the line deregistered
        # until someone happens to notice and toggles it back on by hand.
        # A marker file records the desired state across restarts; empty
        # config.state_dir disables this (persistence is a nice-to-have,
        # never a hard requirement to run).
        self._state_file = os.path.join(config.state_dir, f"{line.id}.registered") if config.state_dir else None

    def _persist(self, registered: bool):
        if not self._state_file:
            return
        try:
            if registered:
                with open(self._state_file, "w") as f:
                    f.write(str(int(time.time())))
            else:
                os.remove(self._state_file)
        except OSError as e:
            print(f"[sip:{self.line.id}] Could not persist registration state: {e!r}")

    def was_registered_before_restart(self) -> bool:
        return bool(self._state_file) and os.path.exists(self._state_file)

    def _build_register(self, cseq, call_id, tag, branch, auth_header=None, expires=None, wildcard_contact=False):
        expires = config.register_expires if expires is None else expires
        return sip_requests.build_register(
            self.line, call_id=call_id, tag=tag, branch=branch, cseq=cseq,
            expires=expires, auth_header=auth_header, wildcard_contact=wildcard_contact)

    def _do_register(self, expires: int, wildcard: bool = False) -> bool:
        line = self.line
        transport = self._transport_holder()
        call_id = f"bridge-reg-{secrets.token_hex(6)}@{line.local_ip}"
        tag = sip_requests.new_tag()
        branch = sip_requests.new_branch()

        msg = self._build_register(1, call_id, tag, branch, expires=expires, wildcard_contact=wildcard)
        transport.send(msg)
        resp_text = transport.wait_response(call_id, timeout=config.sip_response_timeout)
        if resp_text is None:
            self.last_error = "Timeout on REGISTER #1"
            return False

        status_line = resp_text.split("\r\n", 1)[0]
        if " 401 " not in status_line and " 407 " not in status_line:
            if " 200 " in status_line:
                return True
            self.last_error = f"Unexpected response: {status_line}"
            return False

        headers = parse_sip_headers(resp_text)
        challenge = parse_auth_challenge(headers.get("www-authenticate", ""))
        realm, nonce = challenge.get("realm", ""), challenge.get("nonce", "")
        uri = f"sip:{line.gateway_host}"
        response_digest = digest_response(line.sip_user, realm, line.sip_pass, "REGISTER", uri, nonce)
        auth_header = sip_requests.authorization_header(line.sip_user, realm, uri, nonce, response_digest)
        branch2 = sip_requests.new_branch("x2")
        msg2 = self._build_register(2, call_id, tag, branch2, auth_header=auth_header, expires=expires, wildcard_contact=wildcard)
        transport.send(msg2)
        resp2_text = transport.wait_response(call_id, timeout=config.sip_response_timeout)
        if resp2_text is None:
            self.last_error = "Timeout on REGISTER #2 (with auth)"
            return False
        if " 200 " in resp2_text.split("\r\n", 1)[0]:
            self.last_error = None
            return True
        self.last_error = f"Registration failed: {resp2_text.splitlines()[0]}"
        return False

    def _keepalive_loop(self):
        while not self.stop_event.wait(config.register_expires * 0.6):
            with self.lock:
                if not self.registered:
                    return
                ok = self._do_register(config.register_expires)
                if not ok:
                    self.registered = False
                    return

    def wipe_all_bindings(self):
        with self.lock:
            self._do_register(0, wildcard=True)

    def turn_on(self) -> bool:
        with self.lock:
            if self.registered:
                return True
            ok = self._do_register(config.register_expires)
            self.registered = ok
            if ok:
                self._persist(True)
                self.stop_event.clear()
                self.keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True)
                self.keepalive_thread.start()
            return ok

    def turn_off(self, persist: bool = True) -> bool:
        """Deregisters from the gateway. persist=False keeps the stored
        "this line should be registered" state, for shutting down a line
        that is meant to come back: the daemon deregisters on the way out
        so the gateway does not keep sending calls to a dead endpoint, but
        a restart has to bring the line up again by itself."""
        with self.lock:
            if not self.registered:
                if persist:
                    self._persist(False)
                return True
            self.stop_event.set()
            ok = self._do_register(0)
            self.registered = False
            if persist:
                self._persist(False)
            return ok

    def status(self) -> dict:
        return {
            "line": self.line.id,
            "registered": self.registered,
            "username": self.line.sip_user,
            "proxy": f"{self.line.proxy_host}:{self.line.proxy_port}",
            "last_error": self.last_error,
        }
