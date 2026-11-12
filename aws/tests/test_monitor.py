"""Tests for the 10-minute health check."""
import cfd_common as cc
import monitor
from conftest import new_run


def old(seconds):
    """An ISO time this many seconds in the past."""
    import datetime as dt
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_offline_worker_with_waiting_runs_is_reported(aws):
    """Waiting runs plus a silent worker raise one email; a fresh heartbeat or a short wait does not."""
    job_id, _ = new_run(aws)
    aws.jobs.items[job_id].update(status="queued", queued_at=old(1200))
    out = monitor.handler()
    assert out["problems"] == ["worker_offline"] and len(aws.sns.sent) == 1
    assert "python aws/worker/worker.py" in aws.sns.sent[0]["Message"]
    monitor.handler()
    assert len(aws.sns.sent) == 1                                   # the same problem is not repeated
    aws.jobs.items["_worker"] = {"job_id": "_worker", "last_seen": cc.utc_now()}
    assert monitor.handler()["problems"] == []
    aws.jobs.items["_worker"]["last_seen"] = old(1000)
    aws.jobs.items[job_id]["queued_at"] = old(60)
    assert monitor.handler()["problems"] == []


def test_stuck_running_job_is_reported(aws):
    """A running job silent for over 30 minutes is reported by name."""
    job_id, _ = new_run(aws, label="stuckwing")
    aws.jobs.items[job_id].update(status="running", updated_at=old(3600))
    aws.jobs.items["_worker"] = {"job_id": "_worker", "last_seen": cc.utc_now()}
    out = monitor.handler()
    assert out["problems"] == ["stale_running"] and "stuckwing" in aws.sns.sent[0]["Message"]


def test_abandoned_uploads_are_cancelled_quietly(aws):
    """An upload never finished after two hours is cancelled so it stops counting against the user's limit."""
    stale, _ = new_run(aws, label="stale")
    fresh, _ = new_run(aws, label="fresh")
    aws.jobs.items[stale]["created_at"] = old(8000)
    out = monitor.handler()
    assert out["cancelled"] == 1 and aws.jobs.items[stale]["status"] == "cancelled"
    assert aws.jobs.items[fresh]["status"] == "awaiting_upload" and not aws.sns.sent
