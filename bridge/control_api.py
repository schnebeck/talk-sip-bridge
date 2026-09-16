"""Local HTTP control API for the Nextcloud app: status and on/off toggle
for the gateway registration. Not authenticated - relies on being bound to
an address only reachable from the Nextcloud container/host, never a public
interface (see config.control_bind).
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _make_handler(registrar, call_manager):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # avoid interleaving with the daemon's own [daemon]/[call]/[talk] logs

        def _send_json(self, status: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _status_payload(self) -> dict:
            payload = registrar.status()
            payload.update(call_manager.status())
            return payload

        def do_GET(self):
            if self.path == "/status":
                self._send_json(200, self._status_payload())
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):
            if self.path == "/toggle":
                ok = registrar.turn_off() if registrar.registered else registrar.turn_on()
                self._send_json(200 if ok else 502, self._status_payload())
            else:
                self._send_json(404, {"error": "not found"})

    return Handler


def start_in_background(bind: str, port: int, registrar, call_manager) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((bind, port), _make_handler(registrar, call_manager))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[daemon] Control API on {bind}:{port}")
    return server
