"""Helpers for the browser tests: a web server for the real pages and a pretend AWS behind the network.

The pages are served from aws/web with the exact Content-Security-Policy that Terraform sets (read from
terraform/site.tf, so the test and the real policy cannot drift apart). Every request to Cognito, the API and S3
is answered here: the API requests go through the real api.handler, backed by the in-memory tables from conftest.
"""
import base64
import json
import re
import threading
import time
from email import message_from_bytes
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import api
import cfd_common as cc

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
API_ORIGIN = "https://api.example.test"
UPLOADS_ORIGIN = "https://up.s3.eu-west-2.amazonaws.com"
RESULTS_ORIGIN = "https://res.s3.eu-west-2.amazonaws.com"
COGNITO_ORIGIN = "https://cognito-idp.eu-west-2.amazonaws.com"
CLIENT_ID = "testclient"
PASSWORD = "Correct-Horse-9!"
USERS = {"alice@example.com": "sub-alice", "bob@example.com": "sub-bob"}


def terraform_csp() -> str:
    """The Content-Security-Policy from site.tf with the pretend origins filled in."""
    text = (ROOT / "terraform" / "site.tf").read_text()
    block = re.search(r'content_security_policy = join\("; ", \[(.*?)\]\)', text, re.S).group(1)
    policy = "; ".join(re.findall(r'"([^"]+)"', block))
    for old, new in {"${var.region}": "eu-west-2", "${local.results_origin}": RESULTS_ORIGIN,
                     "${aws_apigatewayv2_api.http.api_endpoint}": API_ORIGIN,
                     "${local.uploads_origin}": UPLOADS_ORIGIN}.items():
        policy = policy.replace(old, new)
    assert "${" not in policy, policy
    return policy


class SiteServer:
    """Serves aws/web on a free local port, with the real security headers and a test config.js."""

    def __init__(self):
        """Start serving in a background thread."""
        csp = terraform_csp()

        class Handler(SimpleHTTPRequestHandler):
            """Static files plus config.js."""

            def __init__(self, *a, **k):
                super().__init__(*a, directory=str(WEB), **k)

            def log_message(self, *a):
                pass

            def end_headers(self):
                self.send_header("Content-Security-Policy", csp)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                super().end_headers()

            def do_GET(self):
                if self.path.split("?")[0] == "/config.js":
                    body = ("window.CFD_CONFIG = " + json.dumps({"region": "eu-west-2", "clientId": CLIENT_ID,
                                                                 "apiUrl": API_ORIGIN}) + ";\n").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/javascript")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    super().do_GET()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        """Stop the server."""
        self.httpd.shutdown()


def make_jwt(claims: dict) -> str:
    """An unsigned token with the given claims (the pretend authorizer only reads them)."""
    def part(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


class Cloud:
    """The pretend AWS: Cognito, the API (real handler), the upload bucket and the results bucket."""

    def __init__(self, backends, page_origin: str, png: bytes, mp4: bytes | None):
        """backends: api.Backends with in-memory tables; page_origin: where the pages are served from."""
        self.be, self.origin, self.png, self.mp4 = backends, page_origin, png, mp4
        self.token_seconds = 3600
        self.force_401 = False
        self.upload_error = None
        self.log = []                                # (method, url) of every pretend-cloud request
        self.logins = self.refreshes = 0
        self.upload_orders = []                      # field names of each upload, in order

    # ---- plumbing ----
    def cors(self, extra=None):
        """CORS headers allowing the page's origin (the real API and bucket are configured the same way)."""
        return {"access-control-allow-origin": self.origin,
                "access-control-allow-headers": "authorization,content-type",
                "access-control-allow-methods": "GET,POST,OPTIONS", "access-control-expose-headers": "ETag",
                "vary": "Origin", **(extra or {})}

    def handle(self, route):
        """Answer one browser request; local pages pass through, anything unknown is refused."""
        req = route.request
        url = req.url
        if url.startswith(self.origin):
            route.continue_()
            return
        self.log.append((req.method, url))
        if url.startswith(COGNITO_ORIGIN):
            self.cognito(route)
        elif url.startswith(API_ORIGIN):
            self.api(route)
        elif url.startswith(UPLOADS_ORIGIN):
            self.uploads(route)
        elif url.startswith(RESULTS_ORIGIN):
            self.results(route)
        else:
            route.abort()

    # ---- Cognito ----
    def tokens(self, email):
        """Fresh tokens for a user; the ID token carries what the API's authorizer would check."""
        now = int(time.time())
        claims = {"sub": USERS[email], "email": email, "aud": CLIENT_ID, "exp": now + self.token_seconds}
        return {"AccessToken": make_jwt({**claims, "client_id": CLIENT_ID}), "IdToken": make_jwt(claims),
                "ExpiresIn": self.token_seconds}

    def cognito(self, route):
        """InitiateAuth (password and refresh) and GlobalSignOut."""
        req = route.request
        headers = {"access-control-allow-origin": self.origin, "access-control-allow-headers": "*",
                   "access-control-allow-methods": "POST,OPTIONS"}
        if req.method == "OPTIONS":
            route.fulfill(status=204, headers=headers)
            return
        target = req.headers.get("x-amz-target", "").rsplit(".", 1)[-1]
        body = json.loads(req.post_data or "{}")
        ctype = {"content-type": "application/x-amz-json-1.1", **headers}
        if target == "InitiateAuth" and body["AuthFlow"] == "USER_PASSWORD_AUTH":
            params = body["AuthParameters"]
            if USERS.get(params["USERNAME"]) and params["PASSWORD"] == PASSWORD:
                self.logins += 1
                result = {**self.tokens(params["USERNAME"]), "RefreshToken": "refresh-" + params["USERNAME"]}
                route.fulfill(status=200, headers=ctype, body=json.dumps({"AuthenticationResult": result}))
            else:
                route.fulfill(status=400, headers=ctype, body=json.dumps(
                    {"__type": "NotAuthorizedException", "message": "Incorrect username or password."}))
        elif target == "InitiateAuth" and body["AuthFlow"] == "REFRESH_TOKEN_AUTH":
            self.refreshes += 1
            email = body["AuthParameters"]["REFRESH_TOKEN"].removeprefix("refresh-")
            route.fulfill(status=200, headers=ctype, body=json.dumps({"AuthenticationResult": self.tokens(email)}))
        elif target == "GlobalSignOut":
            route.fulfill(status=200, headers=ctype, body="{}")
        else:
            route.fulfill(status=400, headers=ctype, body=json.dumps({"__type": "InvalidParameterException"}))

    # ---- the API ----
    def api(self, route):
        """Turn the browser request into an API Gateway event, run the real handler, send its answer back."""
        req = route.request
        if req.method == "OPTIONS":
            route.fulfill(status=204, headers=self.cors())
            return
        path = req.url[len(API_ORIGIN):].split("?")[0]
        params, key = {}, None
        m = re.fullmatch(r"/jobs/([0-9a-f]{32})(/submit|/cancel)?", path)
        if path == "/request-access":
            key = "POST /request-access"
        elif path == "/jobs":
            key = f"{req.method} /jobs"
        elif m:
            key, params = {"": "GET /jobs/{id}", "/submit": "POST /jobs/{id}/submit",
                           "/cancel": "POST /jobs/{id}/cancel"}[m.group(2) or ""], {"id": m.group(1)}
            assert key.split()[0] == req.method, (key, req.method)
        assert key, (req.method, path)
        ctx = {"http": {"sourceIp": "203.0.113.9"}}
        if key != "POST /request-access":
            claims = self.check_token(req.headers.get("authorization", ""))
            if claims is None:
                route.fulfill(status=401, headers=self.cors(), body=json.dumps({"message": "Unauthorized"}))
                return
            ctx["authorizer"] = {"jwt": {"claims": claims}}
        out = api.handler({"routeKey": key, "pathParameters": params, "body": req.post_data, "requestContext": ctx})
        route.fulfill(status=out["statusCode"], headers={**out["headers"], **self.cors()}, body=out["body"])

    def check_token(self, header):
        """What the API Gateway authorizer does: a bearer ID token for our client that has not expired."""
        if self.force_401 or not header.startswith("Bearer "):
            return None
        try:
            part = header.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        except (IndexError, ValueError):
            return None
        return claims if claims.get("aud") == CLIENT_ID and claims.get("exp", 0) > time.time() else None

    # ---- S3 ----
    def uploads(self, route):
        """A presigned-form POST: checks the file is the last field and stores it where the form says."""
        req = route.request
        if req.method == "OPTIONS":
            route.fulfill(status=204, headers=self.cors())
            return
        if self.upload_error:
            route.fulfill(status=403, headers=self.cors(), content_type="application/xml",
                          body=f"<Error><Code>{self.upload_error}</Code></Error>")
            return
        ctype = req.headers["content-type"]
        msg = message_from_bytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + req.post_data_buffer)
        names, fields, data = [], {}, b""
        for part in msg.get_payload():
            name = part.get_param("name", header="content-disposition")
            names.append(name)
            if name == "file":
                data = part.get_payload(decode=True)
            else:
                fields[name] = part.get_payload(decode=True).decode()
        self.upload_orders.append(names)
        assert names[-1] == "file" and names.count("file") == 1, names
        limit = self.be.s3.posts[-1]["Conditions"][0]
        assert limit[0] == "content-length-range"
        if not limit[1] <= len(data) <= limit[2]:
            route.fulfill(status=400, headers=self.cors(), content_type="application/xml",
                          body="<Error><Code>EntityTooLarge</Code></Error>")
            return
        self.be.s3.objects[("up", fields["key"])] = data
        route.fulfill(status=204, headers=self.cors({"etag": '"abc"'}))

    def results(self, route):
        """Pictures, video and downloads for finished runs."""
        name = route.request.url.split("?")[0].rsplit("/", 1)[-1]
        if name.endswith(".png"):
            route.fulfill(status=200, content_type="image/png", body=self.png)
        elif name.endswith(".mp4") and self.mp4:
            route.fulfill(status=200, content_type="video/mp4", body=self.mp4)
        else:
            route.fulfill(status=200, content_type="application/octet-stream", body=b"x")


# ---- pretending to be the worker program ----
def worker_online(be):
    """Write the worker's heartbeat the way worker.py does."""
    cc.update_fields(be.jobs, cc.WORKER_KEY, {"last_seen": cc.utc_now()})


def worker_update(be, job_id, **fields):
    """Change a run the way the worker would (status, stage, stages, progress, summary, files...)."""
    return cc.update_fields(be.jobs, {"job_id": job_id}, {"updated_at": cc.utc_now(), **fields})
