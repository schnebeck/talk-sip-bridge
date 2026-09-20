"""The socket one line owns, and the loop that reads it.

Responses are handed to whoever is waiting for that Call-ID; requests are
dispatched to the line's CallManager.
"""
import queue
import socket
import threading

from config import config
from sip_messages import parse_sip_headers


class SipTransport:
    """A single, permanently bound UDP socket for one line: its own
    REGISTER/INVITE transactions AND incoming requests. Its Contact header
    (where calls arrive) points at exactly this port. Only accepts traffic
    from that line's own configured proxy/gateway."""

    def __init__(self, call_manager, line):
        self.line = line
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((line.local_ip, line.local_sip_port))
        self.pending = {}
        self.pending_lock = threading.Lock()
        self.call_manager = call_manager
        self.thread = threading.Thread(target=self._listen_loop, daemon=True)
        self.thread.start()

    def send(self, data: bytes, addr=None):
        self.sock.sendto(data, addr or (self.line.proxy_host, self.line.proxy_port))

    def wait_response(self, call_id: str, timeout: float = None):
        if timeout is None:
            timeout = config.sip_response_timeout
        q = queue.Queue()
        with self.pending_lock:
            self.pending[call_id] = q
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            with self.pending_lock:
                self.pending.pop(call_id, None)

    def open_waiter(self, call_id: str) -> queue.Queue:
        q = queue.Queue()
        with self.pending_lock:
            self.pending[call_id] = q
        return q

    def close_waiter(self, call_id: str):
        with self.pending_lock:
            self.pending.pop(call_id, None)

    def _listen_loop(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(65536)
            except OSError:
                return
            if addr[0] != self.line.proxy_host:
                # Only this line's configured proxy/gateway may send it SIP
                # traffic - anything else on this network could otherwise
                # forge an INVITE, BYE or CANCEL for an existing call.
                print(f"[sip:{self.line.id}] Ignoring packet from unexpected source {addr[0]} (expected {self.line.proxy_host})")
                continue
            text = data.decode(errors="replace")
            if not text.strip():
                continue
            first_line = text.split("\r\n", 1)[0]
            headers = parse_sip_headers(text)
            call_id = headers.get("call-id", "")

            if first_line.startswith("SIP/2.0"):
                with self.pending_lock:
                    q = self.pending.get(call_id)
                if q:
                    q.put(text)
                continue

            method = first_line.split(" ")[0]
            print(f"[sip:{self.line.id}] {method} received for call {call_id}")
            try:
                if method == "INVITE":
                    self.call_manager.handle_invite(text, headers, call_id, addr)
                elif method == "BYE":
                    self.call_manager.handle_bye(text, headers, call_id, addr)
                elif method == "CANCEL":
                    self.call_manager.handle_cancel(text, headers, call_id, addr)
                elif method == "OPTIONS":
                    self.call_manager.handle_options(text, headers, call_id, addr)
            except Exception as e:
                print(f"[sip:{self.line.id}] Error handling {method}: {e}")
