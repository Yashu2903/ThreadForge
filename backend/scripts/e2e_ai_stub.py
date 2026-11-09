"""TEST-ONLY: a local stand-in for the two AI providers, for the browser tests.

    uv run python -m scripts.e2e_ai_stub [--port 8190]

It serves the OpenAI Responses and Gemini embedding paths over plain HTTP on 127.0.0.1 by reusing the
same stub the pytest suite uses (tests/analysis_helpers.py), so the browser tests exercise the REAL
adapters, the REAL worker and the REAL API against deterministic provider behaviour. It proves nothing
about the real providers (docs/ai.md lists what remains unverified).

Refuses to bind anywhere but loopback.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx2

from tests.analysis_helpers import AnalysisStub

STUB = AnalysisStub()


class Handler(BaseHTTPRequestHandler):
    def _serve(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        request = httpx2.Request(
            self.command,
            f"http://stub{self.path}",
            headers=dict(self.headers.items()),
            content=body,
        )
        if self.path.startswith("/v1beta"):
            response = STUB._google(request)  # noqa: SLF001 - the stub's own handlers
        else:
            response = STUB._openai(request)  # noqa: SLF001
        self.send_response(response.status_code)
        for key, value in response.headers.items():
            if key.lower() not in ("content-length", "transfer-encoding", "connection"):
                self.send_header(key, value)
        payload = response.content
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _serve  # noqa: N815

    def log_message(self, *_args: object) -> None:  # silence
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8190)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(json.dumps({"stub": "ready", "port": args.port}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
