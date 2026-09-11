"""Internal-only authenticated Seerr fixture, never a production provider."""

import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = 404, {"message": "Unknown fixture route"}
        if self.path == "/ready":
            status, body = 200, {"fixture_ready": True}
        elif self.path == "/api/v1/settings/public":
            supplied = self.headers.get("X-Api-Key", "")
            digest = hashlib.sha256(supplied.encode()).hexdigest()
            if digest == os.environ["SMOKE_API_KEY_SHA256"]:
                status, body = 200, {"initialized": True, "smoke_authenticated": True}
            else:
                status, body = 401, {"message": "Fixture authentication failed"}
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        # Never echo credentials, headers or arbitrary request data.
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 5055), Handler).serve_forever()
