"""A check that runs every 10 minutes (EventBridge) and emails the owner if the system looks stuck.

It looks for three things:

  * runs waiting in the queue while the worker program on the owner's PC is offline,
  * a running job that has not reported progress for a long time (the worker probably died),
  * uploads that were never finished: these are cancelled quietly so they stop counting against the user's limit.

Problems are emailed at most once every 6 hours each, so a long outage is one email, not a hundred.
"""
from __future__ import annotations

import api
import cfd_common as cc

WORKER_OFFLINE_SECONDS = 300          # the worker must have been silent this long before it is "offline"
QUEUE_WAIT_SECONDS = 600              # ...and a run must have waited this long
STALE_RUNNING_SECONDS = 1800          # a running job silent for this long is considered stuck
ABANDONED_UPLOAD_SECONDS = 7200       # an upload not finished after this long is cancelled
REPEAT_SECONDS = 6 * 3600             # the same problem is emailed at most this often


def find_problems(jobs_queued: list, jobs_running: list, worker: dict) -> dict:
    """Return {problem name: description} for everything that looks wrong right now (empty if all is well)."""
    problems = {}
    silent = worker["last_seen_seconds"]
    offline = silent is None or silent > WORKER_OFFLINE_SECONDS
    waiting = [j for j in jobs_queued if (cc.seconds_since(j.get("queued_at")) or 0) > QUEUE_WAIT_SECONDS]
    if offline and waiting:
        since = "never" if silent is None else f"{silent // 60} minutes ago"
        problems["worker_offline"] = (f"{len(waiting)} run(s) have been waiting more than "
                                      f"{QUEUE_WAIT_SECONDS // 60} minutes, and the worker on your PC last checked in "
                                      f"{since}. Start it (python aws/worker/worker.py), or check the PC is awake.")
    stale = [j for j in jobs_running if (cc.seconds_since(j.get("updated_at")) or 0) > STALE_RUNNING_SECONDS]
    if stale:
        names = ", ".join(f"{j.get('label', '?')} ({j['job_id'][:8]})" for j in stale)
        problems["stale_running"] = (f"These runs show as running but have not reported for over "
                                     f"{STALE_RUNNING_SECONDS // 60} minutes: {names}. The worker may have stopped.")
    return problems


def expire_abandoned_uploads(table, now_jobs: list) -> int:
    """Cancel runs still waiting for an upload long after they were created; returns how many."""
    count = 0
    for job in now_jobs:
        if (cc.seconds_since(job.get("created_at")) or 0) > ABANDONED_UPLOAD_SECONDS:
            now = cc.utc_now()
            try:
                cc.update_fields(table, {"job_id": job["job_id"]},
                                 {"status": cc.CANCELLED, "stage": "Upload not completed",
                                  "message": "The upload was never finished.", "finished_at": now, "updated_at": now},
                                 only_if=("status", (cc.AWAITING_UPLOAD,)))
                count += 1
            except Exception as exc:                                # noqa: BLE001
                if not cc.is_conditional_failure(exc):
                    raise
    return count


def handler(event=None, context=None) -> dict:
    """Entry point for the schedule: run the checks, email any new problem, and report what was found."""
    be, cfg = api.backends(), api.config()
    queued = cc.query_index(be.jobs, "by_status", "status", cc.QUEUED, limit=200)
    running = cc.query_index(be.jobs, "by_status", "status", cc.RUNNING, limit=20)
    awaiting = cc.query_index(be.jobs, "by_status", "status", cc.AWAITING_UPLOAD, limit=200)
    problems = find_problems(queued, running, api.worker_status(be))
    for name, text in problems.items():
        cc.alert_once(be.jobs, be.sns, cfg["topic_arn"], name, f"Needs attention: {name.replace('_', ' ')}",
                      text, min_seconds=REPEAT_SECONDS)
    cancelled = expire_abandoned_uploads(be.jobs, awaiting)
    return {"problems": sorted(problems), "queued": len(queued), "running": len(running), "cancelled": cancelled}
