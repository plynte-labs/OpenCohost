"""Throwaway cloud-endpoint mock for runtime validation (docs/runtime-validation-20260731.md).

NOT part of the test suite — a manual prop: point the provider card's base_url at
http://127.0.0.1:9876/v1 (any model id, any key) and watch the app react.

Modes cover every pending §8 arc:
  python tools/mock_cloud_429.py                     # 2x 429+Retry-After:20, then healthy (§1b rate_limited arc)
  python tools/mock_cloud_429.py --bare              # 2x bare 429, then healthy (ambiguous_429 restore arc)
  python tools/mock_cloud_429.py --bare --fail forever   # bare 429s until Ctrl+C (§8.3 give-up after 6 attempts)
  python tools/mock_cloud_429.py --fail 0            # always healthy (flip to this mid-run to let a probe succeed)
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--port", type=int, default=9876)
parser.add_argument("--bare", action="store_true", help="429 WITHOUT Retry-After (ambiguous_429)")
parser.add_argument("--fail", default="2", help="number of 429s before turning healthy, or 'forever'")
args = parser.parse_args()
FAIL_FOREVER = args.fail == "forever"
FAIL_N = 0 if FAIL_FOREVER else int(args.fail)
count = {"n": 0}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        count["n"] += 1
        if FAIL_FOREVER or count["n"] <= FAIL_N:
            self.send_response(429)
            if not args.bare:
                self.send_header("Retry-After", "20")
            self.end_headers()
            print(f"request {count['n']}: 429 {'bare' if args.bare else '+Retry-After'}")
        else:
            body = json.dumps(
                {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            print(f"request {count['n']}: 200 healthy")

    def log_message(self, *a):  # quiet the default per-request stderr noise
        pass


print(f"mock cloud on 127.0.0.1:{args.port} — bare={args.bare} fail={args.fail}")
HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
