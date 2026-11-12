#!/usr/bin/env python3
"""Owner's command-line tool for the CFD site: approve access requests and look at runs.

    python aws/tools/cfd_admin.py requests            list requests waiting for a decision (--all shows every one)
    python aws/tools/cfd_admin.py approve EMAIL       create the account; Cognito emails a temporary password
    python aws/tools/cfd_admin.py approve EMAIL --resend   send a new temporary password to an existing account
    python aws/tools/cfd_admin.py reject EMAIL        mark a request as declined (nothing is sent to the person)
    python aws/tools/cfd_admin.py jobs                recent runs of all users (--status failed, --limit 50)
    python aws/tools/cfd_admin.py worker              is the worker program on the home PC checking in?

It signs in to AWS with whatever `aws configure` set up (your admin user), and learns the table and pool names from
`terraform output` in aws/terraform, so run it from anywhere inside the repo.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lambda"))
import cfd_common as cc  # noqa: E402


def load_config(path: Path | None) -> dict:
    """Names of the tables and the user pool: from a JSON file if given, otherwise from `terraform output`."""
    if path:
        return json.loads(Path(path).read_text())
    tf_dir = HERE.parent / "terraform"
    try:
        out = subprocess.run(["terraform", "output", "-json", "worker_config"], cwd=tf_dir, check=True,
                             capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Could not read `terraform output` in {tf_dir} ({exc}). "
                         "Run `terraform apply` first, or pass --config FILE.") from None
    return json.loads(out)


# --------------------------------------------------------------------------- #
# The actions (each takes the AWS objects as arguments so tests can pass stand-ins)
# --------------------------------------------------------------------------- #
def list_requests(requests_table, show_all: bool = False) -> list[dict]:
    """Access requests, oldest first; only the undecided ones unless show_all."""
    items, start = [], None
    while True:
        out = requests_table.scan(**({"ExclusiveStartKey": start} if start else {}))
        items += [cc.from_ddb(i) for i in out.get("Items", [])]
        start = out.get("LastEvaluatedKey")
        if not start:
            break
    items = [i for i in items if show_all or i.get("status") == "pending"]
    return sorted(items, key=lambda i: i.get("requested_at", ""))


def approve(email: str, requests_table, cognito, pool_id: str, resend: bool = False) -> str:
    """Create the Cognito account for an email and mark its request approved; returns what happened, in words."""
    email = email.strip().lower()
    args = {"UserPoolId": pool_id, "Username": email, "DesiredDeliveryMediums": ["EMAIL"],
            "UserAttributes": [{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}]}
    if resend:
        args["MessageAction"] = "RESEND"
    try:
        cognito.admin_create_user(**args)
        said = f"Account created for {email}. Cognito has emailed a temporary password (valid 7 days)."
    except Exception as exc:                                        # noqa: BLE001 - one error is expected
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code != "UsernameExistsException":
            raise
        said = f"{email} already has an account. Use --resend to email a new temporary password."
    if requests_table.get_item(Key={"email": email}).get("Item"):
        cc.update_fields(requests_table, {"email": email}, {"status": "approved", "approved_at": cc.utc_now()})
    return said


def reject(email: str, requests_table) -> str:
    """Mark a request declined; the person is not told."""
    email = email.strip().lower()
    if not requests_table.get_item(Key={"email": email}).get("Item"):
        return f"No request from {email}."
    cc.update_fields(requests_table, {"email": email}, {"status": "rejected", "decided_at": cc.utc_now()})
    return f"Request from {email} marked as declined. Nothing was sent to them."


def recent_jobs(jobs_table, status: str | None = None, limit: int = 30) -> list[dict]:
    """The newest runs of all users (optionally in one state), newest first."""
    states = [status] if status else [*cc.ACTIVE, *cc.FINISHED]
    found = []
    for s in states:
        found += cc.query_index(jobs_table, "by_status", "status", s, newest_first=True, limit=limit)
    return sorted(found, key=lambda j: j.get("created_at", ""), reverse=True)[:limit]


def describe_worker(jobs_table) -> str:
    """A sentence about the worker's last check-in."""
    item = jobs_table.get_item(Key=cc.WORKER_KEY).get("Item")
    if not item:
        return "The worker has never checked in."
    item = cc.from_ddb(item)
    age = cc.seconds_since(item.get("last_seen"))
    state = "ONLINE" if age is not None and age <= cc.WORKER_ONLINE_SECONDS else "OFFLINE"
    return (f"Worker {state}: last check-in {int(age or 0)} s ago from {item.get('host', '?')} "
            f"(version {item.get('version', '?')}).")


def main(argv=None) -> int:
    """Parse the command line and run one action."""
    ap = argparse.ArgumentParser(description="Administer the CFD site.")
    ap.add_argument("--config", type=Path, help="JSON file with the terraform worker_config output")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("requests", help="list access requests")
    r.add_argument("--all", action="store_true")
    for name in ("approve", "reject"):
        p = sub.add_parser(name, help=f"{name} a request")
        p.add_argument("email")
        if name == "approve":
            p.add_argument("--resend", action="store_true")
    j = sub.add_parser("jobs", help="list recent runs")
    j.add_argument("--status", choices=[*cc.ACTIVE, *cc.FINISHED])
    j.add_argument("--limit", type=int, default=30)
    sub.add_parser("worker", help="show the worker's last check-in")
    a = ap.parse_args(argv)

    import boto3
    cfg = load_config(a.config)
    db = boto3.resource("dynamodb", region_name=cfg["region"])
    jobs, requests = db.Table(cfg["jobs_table"]), db.Table(cfg["requests_table"])
    if a.cmd == "requests":
        rows = list_requests(requests, a.all)
        for i in rows:
            print(f"{i['requested_at']}  {i['status']:9}  {i['email']}  {i['name']}  {i.get('organisation', '')}\n"
                  f"    {i.get('message', '')}")
        print(f"{len(rows)} request(s).")
    elif a.cmd == "approve":
        print(approve(a.email, requests, boto3.client("cognito-idp", region_name=cfg["region"]),
                      cfg["user_pool_id"], a.resend))
    elif a.cmd == "reject":
        print(reject(a.email, requests))
    elif a.cmd == "jobs":
        for job in recent_jobs(jobs, a.status, a.limit):
            print(f"{job.get('created_at', '')}  {job['status']:15} {job.get('user_email', ''):28} "
                  f"{job.get('label', '')}  {job.get('stage', '')}  {job.get('message', '')[:80]}")
    else:
        print(describe_worker(jobs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
