"""The site's API: one AWS Lambda behind an HTTP API, routed on the route key (for example "GET /jobs").

    POST /request-access       public     someone asks for an account; the owner is emailed (SNS)
    GET  /jobs                 signed in  the caller's runs, newest first, with queue positions
    POST /jobs                 signed in  create 1-5 runs and return one upload form per wing
    POST /jobs/{id}/submit     signed in  the zip has been uploaded: check it and join the queue
    GET  /jobs/{id}            signed in  one run, with download links for its results
    POST /jobs/{id}/cancel     signed in  cancel a run that has not finished

API Gateway has already checked the sign-in token on the signed-in routes; the caller's identity arrives in
the request context. Every job route also checks that the job belongs to the caller. Runs are carried out by
the worker on the owner's PC (worker/worker.py); this API only records, checks and reports.
"""
from __future__ import annotations

import base64
import json
import os
import re
import traceback
import uuid

import cfd_common as cc

MB = 1_000_000
SAFE_FILE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")                      # result file names the worker may publish
EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,}$")
MAX_BODY_BYTES = 50_000


class ApiError(Exception):
    """An error with an HTTP status and a message that is safe to show to the user."""

    def __init__(self, status: int, message: str):
        """status: the HTTP status code; message: what the person sees."""
        super().__init__(message)
        self.status, self.message = status, message


# --------------------------------------------------------------------------- #
# Settings and AWS clients
# --------------------------------------------------------------------------- #
def config() -> dict:
    """Settings from the Lambda's environment (set by Terraform), read on each call so tests can change them."""
    e = os.environ
    return {
        "jobs_table": e.get("JOBS_TABLE", ""), "requests_table": e.get("REQUESTS_TABLE", ""),
        "uploads_bucket": e.get("UPLOADS_BUCKET", ""), "results_bucket": e.get("RESULTS_BUCKET", ""),
        "topic_arn": e.get("TOPIC_ARN", ""),
        "max_upload_bytes": int(float(e.get("MAX_UPLOAD_MB", "100")) * MB),
        "max_step_bytes": int(float(e.get("MAX_STEP_MB", "100")) * MB),
        "max_active": int(e.get("MAX_ACTIVE_JOBS", "10")),
        "max_per_day": int(e.get("MAX_JOBS_PER_DAY", "20")),
    }


class Backends:
    """The four AWS objects the API uses: the jobs and requests tables, S3 and SNS."""

    def __init__(self, jobs, requests, s3, sns):
        """Each argument is a boto3 object (or a test stand-in with the same methods)."""
        self.jobs, self.requests, self.s3, self.sns = jobs, requests, s3, sns


_backends: Backends | None = None


def backends() -> Backends:
    """The AWS objects, created on first use. Importing this module never needs boto3 (tests set _backends)."""
    global _backends
    if _backends is None:
        import boto3
        from botocore.config import Config
        cfg = config()
        db = boto3.resource("dynamodb")
        s3 = boto3.client("s3", config=Config(region_name=os.environ["AWS_REGION"], signature_version="s3v4",
                                              s3={"addressing_style": "virtual"}))
        _backends = Backends(db.Table(cfg["jobs_table"]), db.Table(cfg["requests_table"]), s3, boto3.client("sns"))
    return _backends


# --------------------------------------------------------------------------- #
# Request and response
# --------------------------------------------------------------------------- #
class Request:
    """The parts of an API Gateway event the routes need."""

    def __init__(self, event: dict):
        """event: the HTTP API (payload 2.0) event."""
        ctx = event.get("requestContext") or {}
        self.claims = ((ctx.get("authorizer") or {}).get("jwt") or {}).get("claims") or {}
        self.ip = (ctx.get("http") or {}).get("sourceIp", "")
        self.params = event.get("pathParameters") or {}
        self._event = event

    @property
    def sub(self) -> str:
        """The signed-in user's ID; raises a 401 if the request carries none."""
        sub = self.claims.get("sub")
        if not sub:
            raise ApiError(401, "Please sign in again.")
        return str(sub)

    def json(self) -> dict:
        """The request body as a dictionary (empty if there is none); raises a 400 if it is not a JSON object."""
        body = self._event.get("body") or ""
        if self._event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8", "replace")
        if len(body) > MAX_BODY_BYTES:
            raise ApiError(413, "That request is too large.")
        if not body:
            return {}
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            raise ApiError(400, "The request was not valid JSON.") from None
        if not isinstance(data, dict):
            raise ApiError(400, "The request must be a JSON object.")
        return data


def respond(status: int, body: dict) -> dict:
    """An API Gateway response; nothing is cached because job state changes."""
    return {"statusCode": status, "body": json.dumps(body),
            "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"}}


# --------------------------------------------------------------------------- #
# Job helpers
# --------------------------------------------------------------------------- #
PUBLIC_FIELDS = ("batch_id", "seq", "label", "filename", "options", "status", "stage", "stages", "progress",
                 "created_at",
                 "queued_at", "started_at", "finished_at", "updated_at", "message", "summary", "files",
                 "cancel_requested")


def public_job(item: dict, runs_ahead: int | None = None) -> dict:
    """The fields of a job the browser may see (never the owner's ID or the storage path)."""
    out = {k: item[k] for k in PUBLIC_FIELDS if k in item}
    out["id"] = item["job_id"]
    if runs_ahead is not None:
        out["runs_ahead"] = runs_ahead
    return out


def owned_job(req: Request, be: Backends) -> dict:
    """The job named in the URL if it belongs to the caller. Anyone else's job looks like a missing one."""
    job_id = str(req.params.get("id", ""))
    got = be.jobs.get_item(Key={"job_id": job_id}).get("Item") if re.fullmatch(r"[0-9a-f]{32}", job_id) else None
    job = cc.from_ddb(got) if got else None
    if not job or job.get("user_sub") != req.sub:
        raise ApiError(404, "That run was not found.")
    return job


def queue_state(be: Backends) -> dict:
    """Where each queued job stands: the number of runs ahead of it (the running one plus earlier queued ones)."""
    queued = cc.queue_order(cc.query_index(be.jobs, "by_status", "status", cc.QUEUED, limit=200))
    running = len(cc.query_index(be.jobs, "by_status", "status", cc.RUNNING, limit=20))
    return {j["job_id"]: running + i for i, j in enumerate(queued)}


def worker_status(be: Backends) -> dict:
    """Whether the worker program on the owner's PC has checked in recently."""
    item = be.jobs.get_item(Key=cc.WORKER_KEY).get("Item")
    age = cc.seconds_since(cc.from_ddb(item).get("last_seen")) if item else None
    return {"online": age is not None and age <= cc.WORKER_ONLINE_SECONDS,
            "last_seen_seconds": None if age is None else int(age)}


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def request_access(req: Request) -> dict:
    """Store an access request and email the owner. Always answers the same, so the form reveals nothing about
    which emails already have an account or request.
    """
    body, cfg, be = req.json(), config(), backends()
    reply = {"ok": True, "message": "Thank you. If your request is approved you will get an email with a temporary "
                                    "password."}
    if cc.clean_text(body.get("website"), 200):                      # a hidden field only a robot fills in
        return reply
    email = cc.clean_text(body.get("email"), 254).lower()
    name = cc.clean_text(body.get("name"), 100)
    if not EMAIL.fullmatch(email):
        raise ApiError(400, "Please enter a valid email address.")
    if len(name) < 2:
        raise ApiError(400, "Please enter your name.")
    item = {"email": email, "name": name, "organisation": cc.clean_text(body.get("organisation"), 100),
            "message": cc.clean_text(body.get("message"), 1000), "status": "pending",
            "requested_at": cc.utc_now(), "source_ip": req.ip}
    try:
        be.requests.put_item(Item=item, ConditionExpression="attribute_not_exists(email)")
    except Exception as exc:                                        # noqa: BLE001 - only one error is expected
        if cc.is_conditional_failure(exc):
            return reply                                            # already asked: say nothing, send nothing
        raise
    text = (f"Name: {name}\nEmail: {email}\nOrganisation: {item['organisation'] or '-'}\n"
            f"Message: {item['message'] or '-'}\n\nTo approve:\n  python aws/tools/cfd_admin.py approve {email}\n"
            f"To decline:\n  python aws/tools/cfd_admin.py reject {email}\n")
    cc.send_alert(be.sns, cfg["topic_arn"], f"Access request from {name}", text)   # the request is saved either way
    return reply


def list_jobs(req: Request) -> dict:
    """The caller's runs (newest first) with queue positions, the worker's status and the site's limits."""
    be, cfg = backends(), config()
    mine = cc.query_index(be.jobs, "by_user", "user_sub", req.sub, newest_first=True, limit=50)
    ahead = queue_state(be) if any(j["status"] == cc.QUEUED for j in mine) else {}
    return {"jobs": [public_job(j, ahead.get(j["job_id"])) for j in mine],
            "worker": worker_status(be),
            "limits": {"max_files": cc.MAX_FILES_PER_BATCH, "max_upload_mb": cfg["max_upload_bytes"] // MB,
                       "max_step_mb": cfg["max_step_bytes"] // MB, "max_active": cfg["max_active"],
                       "speed_min": cc.SPEED_RANGE[0], "speed_max": cc.SPEED_RANGE[1]}}


def create_jobs(req: Request) -> dict:
    """Create one run per wing (1-5) and return a presigned upload form for each wing's zip."""
    be, cfg, body = backends(), config(), req.json()
    sub = req.sub
    try:
        settings = cc.validate_settings(body.get("settings", {}))
        raw = body.get("files")
        if not isinstance(raw, list) or not 1 <= len(raw) <= cc.MAX_FILES_PER_BATCH:
            raise cc.ValidationError(f"Choose between 1 and {cc.MAX_FILES_PER_BATCH} wings.")
        files = [cc.validate_file(f, cfg["max_upload_bytes"]) for f in raw]
    except cc.ValidationError as exc:
        raise ApiError(400, str(exc)) from None
    mine = cc.query_index(be.jobs, "by_user", "user_sub", sub, newest_first=True, limit=200)
    active = sum(1 for j in mine if j["status"] in cc.ACTIVE)
    last_day = sum(1 for j in mine if (cc.seconds_since(j.get("created_at")) or 1e9) < 86400)
    if active + len(files) > cfg["max_active"]:
        raise ApiError(429, f"You can have at most {cfg['max_active']} unfinished runs; you have {active}.")
    if last_day + len(files) > cfg["max_per_day"]:
        raise ApiError(429, f"The limit is {cfg['max_per_day']} runs in 24 hours; you have started {last_day}.")
    now, batch, out = cc.utc_now(), uuid.uuid4().hex[:12], []
    for seq, f in enumerate(files):
        job_id = uuid.uuid4().hex
        item = {"job_id": job_id, "batch_id": batch, "seq": seq, "user_sub": sub,
                "user_email": req.claims.get("email", ""),
                "label": f["label"], "filename": f["filename"], "size": f["size"],
                "options": cc.case_options(settings, f), "status": cc.AWAITING_UPLOAD,
                "stage": "Waiting for the upload", "stages": {}, "progress": {}, "message": "",
                "created_at": now, "updated_at": now, "upload_key": f"uploads/{job_id}/wing.zip"}
        be.jobs.put_item(Item=cc.to_ddb(item), ConditionExpression="attribute_not_exists(job_id)")
        form = be.s3.generate_presigned_post(Bucket=cfg["uploads_bucket"], Key=item["upload_key"],
                                             Conditions=[["content-length-range", 1, cfg["max_upload_bytes"]]],
                                             ExpiresIn=900)
        out.append({"job": public_job(item), "upload": {"url": form["url"], "fields": form["fields"]}})
    return {"batch_id": batch, "jobs": out}


def submit_job(req: Request) -> dict:
    """The browser says the zip is uploaded: check it, then put the run in the queue (or reject it with a reason)."""
    be, cfg = backends(), config()
    job = owned_job(req, be)
    if job["status"] == cc.QUEUED:
        return {"job": public_job(job)}                             # a repeated click changes nothing
    if job["status"] != cc.AWAITING_UPLOAD:
        raise ApiError(409, "This run is not waiting for an upload.")
    try:
        size = be.s3.head_object(Bucket=cfg["uploads_bucket"], Key=job["upload_key"])["ContentLength"]
    except Exception as exc:                                        # noqa: BLE001 - only "not there" is expected
        if str(getattr(exc, "response", {}).get("Error", {}).get("Code")) in ("404", "NoSuchKey", "NotFound"):
            raise ApiError(409, "The upload has not arrived yet. Try again in a moment.") from None
        raise

    def fetch(first: int, last: int) -> bytes:
        """Download bytes first..last of the uploaded zip."""
        got = be.s3.get_object(Bucket=cfg["uploads_bucket"], Key=job["upload_key"], Range=f"bytes={first}-{last}")
        return got["Body"].read()

    now = cc.utc_now()
    try:
        info = cc.inspect_step_zip(fetch, size, cfg["max_step_bytes"])
        change = {"status": cc.QUEUED, "stage": "Waiting in the queue", "queued_at": now, "updated_at": now,
                  "step_name": cc.clean_text(info["entry"].rsplit("/", 1)[-1], 120), "step_bytes": info["uncompressed"]}
    except cc.ValidationError as exc:
        change = {"status": cc.FAILED, "stage": "Upload rejected", "message": str(exc), "finished_at": now,
                  "updated_at": now}
    try:
        job = cc.update_fields(be.jobs, {"job_id": job["job_id"]}, change, only_if=("status", (cc.AWAITING_UPLOAD,)))
    except Exception as exc:                                        # noqa: BLE001
        if not cc.is_conditional_failure(exc):
            raise
        job = owned_job(req, be)                                    # someone else (a second click) got there first
    return {"job": public_job(job)}


def get_job(req: Request) -> dict:
    """One run in full, with short-lived download links for its result files."""
    be, cfg = backends(), config()
    job = owned_job(req, be)
    files = []
    for f in job.get("files", []):
        name = str(f.get("name", ""))
        if not SAFE_FILE.fullmatch(name):
            continue
        params = {"Bucket": cfg["results_bucket"], "Key": f"results/{job['job_id']}/{name}"}
        if f.get("kind") in ("report", "archive"):
            params["ResponseContentDisposition"] = f'attachment; filename="{name}"'
        files.append({**f, "url": be.s3.generate_presigned_url("get_object", Params=params, ExpiresIn=900)})
    out = public_job(job, queue_state(be).get(job["job_id"]) if job["status"] == cc.QUEUED else None)
    out["files"] = files
    return {"job": out}


def cancel_job(req: Request) -> dict:
    """Cancel a run. One that has not started is cancelled at once; a running one is flagged for the worker."""
    be = backends()
    job, now = owned_job(req, be), cc.utc_now()
    key = {"job_id": job["job_id"]}
    try:
        if job["status"] in (cc.AWAITING_UPLOAD, cc.QUEUED):
            change = {"status": cc.CANCELLED, "stage": "Cancelled", "finished_at": now, "updated_at": now}
            job = cc.update_fields(be.jobs, key, change, only_if=("status", (cc.AWAITING_UPLOAD, cc.QUEUED)))
        elif job["status"] == cc.RUNNING:
            job = cc.update_fields(be.jobs, key, {"cancel_requested": True, "updated_at": now},
                                   only_if=("status", (cc.RUNNING,)))
        else:
            raise ApiError(409, "This run has already finished.")
    except ApiError:
        raise
    except Exception as exc:                                        # noqa: BLE001
        if not cc.is_conditional_failure(exc):
            raise
        raise ApiError(409, "The run changed state; refresh and try again.") from None
    return {"job": public_job(job)}


ROUTES = {"POST /request-access": request_access, "GET /jobs": list_jobs, "POST /jobs": create_jobs,
          "POST /jobs/{id}/submit": submit_job, "GET /jobs/{id}": get_job, "POST /jobs/{id}/cancel": cancel_job}


def handler(event: dict, context=None) -> dict:
    """The Lambda entry point: find the route, run it, and turn any error into a JSON response."""
    route = ROUTES.get(event.get("routeKey", ""))
    try:
        if route is None:
            raise ApiError(404, "Not found.")
        return respond(200, route(Request(event)))
    except ApiError as exc:
        return respond(exc.status, {"error": exc.message})
    except Exception:                                               # noqa: BLE001 - never leak internals
        trace = traceback.format_exc()[-1500:]
        print(json.dumps({"error": "unhandled", "route": event.get("routeKey"), "trace": trace}))
        try:
            be = backends()
            cc.alert_once(be.jobs, be.sns, config()["topic_arn"], "api_error", "The site's API hit an error",
                          f"Route: {event.get('routeKey')}\n\n{trace}\n\nMore detail: CloudWatch logs for the API "
                          "function. Further errors are not emailed for 10 minutes.")
        except Exception:                                           # noqa: BLE001 - alerting is best effort
            pass
        return respond(500, {"error": "Something went wrong. Please try again."})
