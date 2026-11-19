"""The request handler and server, assembled from their parts."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import signal
import threading
import traceback

from api.routes import ApiMixin
from common.constants import MAX_FORM_BYTES
from web.actions.dispatcher import ActionsMixin
from web.auth_pages import AuthPagesMixin
from web.base import HandlerBase
from web.downloads import DownloadsMixin
from web.router import RouterMixin

REQUEST_TIMEOUT_SECONDS = 30  # a connection that sends or receives nothing for this long is closed

# Each connection is read and answered on its own thread, so a slow or stalled visitor delays nobody else.
# The payroll work itself still runs one request at a time, which keeps the figures and the database consistent.
APP_LOCK = threading.Lock()

class PayrollHandler(RouterMixin, ActionsMixin, ApiMixin, DownloadsMixin, AuthPagesMixin, HandlerBase, BaseHTTPRequestHandler):
    """Every web page, form and mobile API request passes through here."""

    timeout = REQUEST_TIMEOUT_SECONDS

    def read_whole_body(self):
        """Receive the request body before taking a turn, so a stalled upload never holds up other people."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.headers.replace_header("Content-Length", "0")
            length = 0
        if length > MAX_FORM_BYTES:  # too large to keep: discard it, and the form reader reports the rejection
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                remaining -= len(chunk)
            self.rfile = io.BytesIO()
        else:
            self.rfile = io.BytesIO(self.rfile.read(length) if length > 0 else b"")

    def take_turn(self, action):
        """Do the work while holding the lock, then send the finished reply after releasing it."""
        try:
            self.read_whole_body()
        except (TimeoutError, ConnectionError):
            self.close_connection = True
            return
        connection, self.wfile = self.wfile, io.BytesIO()
        with APP_LOCK:
            try:
                action(self)
            except Exception:
                traceback.print_exc()
                self.wfile = io.BytesIO()
                self._headers_buffer = []
                self.send_error(500, "Something went wrong. Please try again.")
        reply, self.wfile = self.wfile.getvalue(), connection
        try:
            self.wfile.write(reply)
        except (TimeoutError, ConnectionError):
            self.close_connection = True

    def do_GET(self):
        self.take_turn(RouterMixin.do_GET)

    def do_POST(self):
        self.take_turn(RouterMixin.do_POST)

    def do_OPTIONS(self):
        self.take_turn(ApiMixin.do_OPTIONS)

class PayrollServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

def run_server(host, port):
    """Serve until told to stop. Work already in progress finishes first, so an update never cuts a save in half."""
    server = PayrollServer((host, port), PayrollHandler)

    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()  # shutdown() can't be called from the serving thread

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.serve_forever()
    APP_LOCK.acquire()  # waits for the request being worked on; none can start after this
    server.server_close()
    print("Payroll server stopped cleanly.")
