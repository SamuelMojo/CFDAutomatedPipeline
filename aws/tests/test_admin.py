"""Tests for the owner's admin tool, using in-memory stand-ins."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import cfd_admin as ad  # noqa: E402
import cfd_common as cc  # noqa: E402
from conftest import FakeClientError, FakeTable  # noqa: E402


class FakeCognito:
    """Records admin_create_user calls; an email already used raises UsernameExistsException."""

    def __init__(self):
        """Start with no users."""
        self.users, self.calls = set(), []

    def admin_create_user(self, **kw):
        """Create the user or raise like Cognito does."""
        self.calls.append(kw)
        if kw["Username"] in self.users and "MessageAction" not in kw:
            raise FakeClientError("UsernameExistsException")
        self.users.add(kw["Username"])


class ScanTable(FakeTable):
    """The requests table with scan(), which the admin tool uses."""

    def scan(self, **kw):
        """Return every item."""
        return {"Items": list(self.items.values())}


def requests_table():
    """A requests table with one pending and one decided request."""
    t = ScanTable(key="email", indexes={})
    t.put_item(Item={"email": "a@x.co", "name": "Ann", "status": "pending", "requested_at": "2026-11-12T10:00:00Z"})
    t.put_item(Item={"email": "b@x.co", "name": "Bob", "status": "rejected", "requested_at": "2026-11-11T10:00:00Z"})
    return t


def test_list_shows_only_pending_unless_all():
    """The default list is the undecided requests, oldest first."""
    t = requests_table()
    assert [i["email"] for i in ad.list_requests(t)] == ["a@x.co"]
    assert [i["email"] for i in ad.list_requests(t, show_all=True)] == ["b@x.co", "a@x.co"]


def test_approve_creates_the_account_and_marks_the_request():
    """Approving asks Cognito to email a temporary password for a verified address and records the decision."""
    t, cog = requests_table(), FakeCognito()
    said = ad.approve(" A@X.co ", t, cog, "pool-1")
    call = cog.calls[0]
    assert "created" in said and call["Username"] == "a@x.co" and call["DesiredDeliveryMediums"] == ["EMAIL"]
    assert {"Name": "email_verified", "Value": "true"} in call["UserAttributes"] and call["UserPoolId"] == "pool-1"
    assert t.items["a@x.co"]["status"] == "approved" and "approved_at" in t.items["a@x.co"]


def test_approve_twice_explains_and_resend_works():
    """A second approval says the account exists; --resend asks Cognito to resend the temporary password."""
    t, cog = requests_table(), FakeCognito()
    ad.approve("a@x.co", t, cog, "p")
    assert "already has an account" in ad.approve("a@x.co", t, cog, "p")
    ad.approve("a@x.co", t, cog, "p", resend=True)
    assert cog.calls[-1]["MessageAction"] == "RESEND"


def test_approve_without_a_request_still_invites():
    """The owner may invite someone who never filled in the form."""
    t, cog = requests_table(), FakeCognito()
    assert "created" in ad.approve("new@x.co", t, cog, "p") and "new@x.co" not in t.items


def test_reject_and_unknown():
    """Rejecting records the decision; an unknown email is reported, not invented."""
    t = requests_table()
    assert "declined" in ad.reject("A@x.co", t) and t.items["a@x.co"]["status"] == "rejected"
    assert ad.reject("nobody@x.co", t) == "No request from nobody@x.co."


def test_recent_jobs_and_worker_text():
    """Runs of all users are listed newest first; the worker line says online or offline."""
    jobs = FakeTable()
    for i, (status, user) in enumerate([("done", "u1"), ("failed", "u2"), ("queued", "u1")]):
        jobs.put_item(Item={"job_id": f"j{i}", "status": status, "user_sub": user,
                            "created_at": f"2026-11-12T10:0{i}:00Z"})
    assert [j["job_id"] for j in ad.recent_jobs(jobs)] == ["j2", "j1", "j0"]
    assert [j["job_id"] for j in ad.recent_jobs(jobs, status="failed")] == ["j1"]
    assert ad.describe_worker(jobs) == "The worker has never checked in."
    jobs.put_item(Item={**cc.WORKER_KEY, "last_seen": cc.utc_now(), "host": "pc", "version": "1"})
    assert ad.describe_worker(jobs).startswith("Worker ONLINE")
