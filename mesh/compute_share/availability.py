"""
This instance's own free/busy status, tracked and served completely
outside the main A2A/asyncio server - a real OS thread with its own tiny
HTTP listener on its own port, not a coroutine sharing the same event
loop that handles run_inference/offload/gossip.

Why this matters and a plain in-process counter (like
mesh/inference_router/skills/complete.py's own local-call counter)
doesn't: that one only ever answers for the SAME process that's asking,
from the SAME event loop, so it's never actually blocked behind
anything. This one has to answer a genuinely different machine, over the
network, and if the main A2A server's event loop were itself busy or
momentarily stalled handling a real inference call, a status check
routed through that same loop could queue up behind it too - exactly
the kind of slow/wrong answer offload.py's availability race depends on
NOT getting. Running the status server on its own thread and its own
socket means it keeps answering instantly regardless of what the main
server is doing.

mark_busy()/mark_free() are called from run_inference.py, synchronously,
protected by the same lock this thread's HTTP handler reads through -
correct under real concurrent access from both a coroutine (the asyncio
loop) and the status thread, not just in the common case.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCAL_CONCURRENCY_LIMIT = 1

_lock = threading.Lock()
_in_flight = 0


def mark_busy() -> None:
    global _in_flight
    with _lock:
        _in_flight += 1


def mark_free() -> None:
    global _in_flight
    with _lock:
        _in_flight -= 1


def is_available() -> bool:
    with _lock:
        return _in_flight < LOCAL_CONCURRENCY_LIMIT


class _AvailabilityHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != '/available':
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({'available': is_available()}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass  # BaseHTTPRequestHandler logs every request to stderr by default - too noisy for a per-request-raced endpoint


def start_server(host: str, port: int) -> None:
    """Starts the availability HTTP listener on a daemon thread - never
    joined, never blocks process shutdown. Safe to call once at agent
    startup, before the main A2A server's own serve() call (which
    blocks) - this one doesn't."""
    server = ThreadingHTTPServer((host, port), _AvailabilityHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name='compute_share_availability')
    thread.start()
