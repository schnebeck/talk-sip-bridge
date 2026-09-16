"""Local HTTP control API for the Nextcloud app: status and on/off toggle
for each line's gateway registration. Not authenticated - relies on being
bound to an address only reachable from the Nextcloud container/host, never
a public interface (see config.control_bind).

With a single configured line (the common case), GET /status and POST
/toggle behave exactly as with one flat line: the response carries that
line's fields directly at the top level, unchanged from before multi-line
support existed. With several lines, add ?line=<id> to target a specific
one (POST without it targets the first configured line); GET /status always
also includes a full "lines" array.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def _make_handler(lines: dict):
    # lines: {line_id: (registrar, call_manager)}
    first_line_id = next(iter(lines), None)

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

        def _line_status(self, line_id: str) -> dict:
            registrar, call_manager = lines[line_id]
            payload = registrar.status()
            payload.update(call_manager.status())
            return payload

        def _all_status(self) -> dict:
            result = {"lines": [self._line_status(lid) for lid in lines]}
            if len(lines) == 1:
                result.update(result["lines"][0])
            return result

        def _requested_line(self):
            query = parse_qs(urlparse(self.path).query)
            return query.get("line", [None])[0] or first_line_id

        def do_GET(self):
            if urlparse(self.path).path == "/status":
                self._send_json(200, self._all_status())
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path
            line_id = self._requested_line()
            if line_id not in lines:
                self._send_json(404, {"error": f"unknown line {line_id!r}" if line_id else "no line configured"})
                return
            registrar, call_manager = lines[line_id]
            if path == "/toggle":
                ok = registrar.turn_off() if registrar.registered else registrar.turn_on()
                self._send_json(200 if ok else 502, self._all_status())
            elif path == "/hangup":
                # Manual escape hatch: ends whatever call is currently
                # active on this line. Call-end detection on the Talk side
                # is best effort (see docs/CONCEPT.md) - this lets a stuck
                # call be cleared without restarting the whole daemon.
                call_manager.hangup()
                self._send_json(200, self._all_status())
            else:
                self._send_json(404, {"error": "not found"})

    return Handler


def start_in_background(bind: str, port: int, lines: dict) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((bind, port), _make_handler(lines))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[daemon] Control API on {bind}:{port} ({len(lines)} line(s): {', '.join(lines)})")
    return server
