"""The socket one line owns, and the loop that reads it.

Responses go to whoever is waiting for that Call-ID; requests are
dispatched to the line's CallManager. Two transports, chosen per line:

- **UDP** - one bound socket, one message per datagram, and a reply goes
  to the address it came from.
- **TCP** - a stream in each direction. Message boundaries have to be
  found (see sip_messages.split_messages), a reply has to go back on the
  connection its request arrived on, and the connection has to be
  re-established after the far end closes it. Registrars that accept
  nothing else are common; this deployment's FritzBox is one.

Both present the same interface to the registrar and the call manager:
`send`, `wait_response`, `open_waiter`, `close_waiter`. What `send`'s
`addr` means differs - an address for UDP, a connection for TCP - and it
is only ever passed back the way it was received.
"""
import queue
import socket
import threading

from config import config
from sip_messages import parse_sip_headers, split_messages

# How long to wait before reconnecting a TCP transport whose connection
# went away. Short: a line without a connection cannot be called.
RECONNECT_DELAY = 2


class _SipTransportBase:
    """Waiter bookkeeping and dispatch, shared by both transports."""

    def __init__(self, call_manager, line):
        self.line = line
        self.call_manager = call_manager
        self.pending = {}
        self.pending_lock = threading.Lock()

    def wait_response(self, call_id: str, timeout: float = None):
        if timeout is None:
            timeout = config.sip_response_timeout
        q = self.open_waiter(call_id)
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            self.close_waiter(call_id)

    def open_waiter(self, call_id: str) -> queue.Queue:
        q = queue.Queue()
        with self.pending_lock:
            self.pending[call_id] = q
        return q

    def close_waiter(self, call_id: str):
        with self.pending_lock:
            self.pending.pop(call_id, None)

    def _handle(self, text: str, reply_to):
        """One complete message: a response goes to its waiter, a request to
        the call manager. `reply_to` is whatever this transport needs to
        answer on."""
        if not text.strip():
            return
        first_line = text.split("\r\n", 1)[0]
        headers = parse_sip_headers(text)
        call_id = headers.get("call-id", "")

        if first_line.startswith("SIP/2.0"):
            with self.pending_lock:
                q = self.pending.get(call_id)
            if q:
                q.put(text)
            return

        method = first_line.split(" ")[0]
        print(f"[sip:{self.line.id}] {method} received for call {call_id}")
        handler = {
            "INVITE": self.call_manager.handle_invite,
            "BYE": self.call_manager.handle_bye,
            "CANCEL": self.call_manager.handle_cancel,
            "OPTIONS": self.call_manager.handle_options,
        }.get(method)
        if handler is None:
            return
        try:
            handler(text, headers, call_id, reply_to)
        except Exception as e:
            print(f"[sip:{self.line.id}] Error handling {method}: {e}")


class UdpSipTransport(_SipTransportBase):
    """A single, permanently bound UDP socket for one line: its own
    REGISTER/INVITE transactions AND incoming requests. Its Contact header
    (where calls arrive) points at exactly this port. Only accepts traffic
    from that line's own configured proxy/gateway."""

    def __init__(self, call_manager, line):
        super().__init__(call_manager, line)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((line.local_ip, line.local_sip_port))
        self.thread = threading.Thread(target=self._listen_loop, daemon=True)
        self.thread.start()

    def send(self, data: bytes, addr=None):
        self.sock.sendto(data, addr or (self.line.proxy_host, self.line.proxy_port))

    def close(self):
        self.sock.close()

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
            self._handle(data.decode(errors="replace"), addr)


class TcpSipTransport(_SipTransportBase):
    """One outgoing connection for this line's own requests, and a listener
    for the ones that come to it.

    Both are needed. A registrar delivers a call to the address in the
    Contact header, and opens a new connection to do it rather than
    answering down the one the registration arrived on - measured against
    this deployment's gateway. So registering is not enough to be
    reachable; something has to be listening."""

    def __init__(self, call_manager, line):
        super().__init__(call_manager, line)
        self._out = None
        self._out_lock = threading.Lock()
        self._closed = False
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((line.local_ip, line.local_sip_port))
        self.listener.listen(8)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    # -- outgoing --------------------------------------------------------
    def _connect(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(config.sip_response_timeout)
        sock.connect((self.line.proxy_host, self.line.proxy_port))
        sock.settimeout(None)
        threading.Thread(target=self._read_loop, args=(sock, "gateway"), daemon=True).start()
        print(f"[sip:{self.line.id}] TCP connection to {self.line.proxy_host}:{self.line.proxy_port} established")
        return sock

    def send(self, data: bytes, addr=None):
        """addr is a connection when answering a request that arrived on
        one; without it the line's own outgoing connection is used, opened
        on first use and re-opened after the far end closes it."""
        if addr is not None and hasattr(addr, "sendall"):
            addr.sendall(data)
            return
        with self._out_lock:
            for attempt in (1, 2):
                try:
                    if self._out is None:
                        self._out = self._connect()
                    self._out.sendall(data)
                    return
                except OSError as e:
                    # A connection the far end closed while idle looks fine
                    # until something is written to it, so one retry on a
                    # fresh connection is part of normal operation.
                    print(f"[sip:{self.line.id}] Send over TCP failed ({e!r})"
                          + (", reconnecting" if attempt == 1 else ""))
                    try:
                        self._out.close()
                    except OSError:
                        pass
                    self._out = None
                    if attempt == 2:
                        raise

    def close(self):
        self._closed = True
        with self._out_lock:
            if self._out is not None:
                self._out.close()
                self._out = None
        self.listener.close()

    # -- incoming --------------------------------------------------------
    def _accept_loop(self):
        while not self._closed:
            try:
                sock, addr = self.listener.accept()
            except OSError:
                return
            if addr[0] != self.line.proxy_host:
                # Same rule as the UDP transport: only this line's own
                # proxy/gateway may send it SIP traffic.
                print(f"[sip:{self.line.id}] Refusing TCP connection from unexpected source {addr[0]} (expected {self.line.proxy_host})")
                sock.close()
                continue
            threading.Thread(target=self._read_loop, args=(sock, addr[0]), daemon=True).start()

    def _read_loop(self, sock, origin):
        """Reassembles the stream into messages. Whatever arrives on this
        connection is answered on it."""
        buffer = b""
        try:
            while not self._closed:
                chunk = sock.recv(65536)
                if not chunk:
                    return  # far end closed
                buffer += chunk
                messages, buffer = split_messages(buffer)
                for raw in messages:
                    self._handle(raw.decode(errors="replace"), sock)
        except OSError:
            return
        finally:
            with self._out_lock:
                if sock is self._out:
                    self._out = None
            try:
                sock.close()
            except OSError:
                pass


def SipTransport(call_manager, line):
    """The transport this line is configured for."""
    if getattr(line, "sip_transport", "udp") == "tcp":
        return TcpSipTransport(call_manager, line)
    return UdpSipTransport(call_manager, line)
