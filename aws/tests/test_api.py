"""Tests for the API Lambda, run against in-memory stand-ins for DynamoDB, S3 and SNS."""
import json

import api
import cfd_common as cc
from conftest import STEP_TEXT, call, event, make_zip, new_run

SETTINGS = {"solver": "SST", "preset": "quick", "speed": 30, "ground": "moving"}


def files(n, **extra):
    """n valid file entries."""
    return [{"filename": f"wing{i}.step", "size": 400, "units": "mm", **extra} for i in range(n)]


# ---- creating runs ---------------------------------------------------------
def test_create_five_wings_in_one_upload(aws):
    """Five wings make five runs in one batch, each with its own signed upload form limited in size."""
    st, out = call("POST /jobs", {"settings": SETTINGS, "files": files(5)})
    assert st == 200 and len(out["jobs"]) == 5 and len({j["job"]["id"] for j in out["jobs"]}) == 5
    job = out["jobs"][0]["job"]
    assert job["status"] == "awaiting_upload" and job["options"]["solver"] == "SST" and job["label"] == "wing0"
    assert "user_sub" not in job and "upload_key" not in job
    form = out["jobs"][0]["upload"]
    assert form["url"].startswith("https://up.s3.") and form["fields"]["key"].startswith("uploads/")
    assert all(p["Conditions"] == [["content-length-range", 1, 1_000_000]] for p in aws.s3.posts)
    assert len(aws.jobs.items) == 5 and {i["batch_id"] for i in aws.jobs.items.values()} == {out["batch_id"]}


def test_create_refuses_bad_requests(aws):
    """Zero wings, six wings, bad settings and oversize zips are all 400s that name the problem."""
    for body in ({"settings": SETTINGS, "files": []}, {"settings": SETTINGS, "files": files(6)},
                 {"settings": {"solver": "nope"}, "files": files(1)},
                 {"settings": SETTINGS, "files": [{"filename": "a.zip", "size": 5_000_000, "units": "mm"}]},
                 {"settings": SETTINGS, "files": [{"filename": "a.zip", "size": 5}]}, {}):
        st, out = call("POST /jobs", body)
        assert st == 400 and out["error"], body
    assert not aws.jobs.items


def test_create_enforces_the_limits(aws):
    """At most 6 unfinished runs, and 8 in 24 hours (the test settings)."""
    assert call("POST /jobs", {"settings": SETTINGS, "files": files(5)})[0] == 200
    st, out = call("POST /jobs", {"settings": SETTINGS, "files": files(2)})
    assert st == 429 and "unfinished" in out["error"]
    for item in aws.jobs.items.values():
        item["status"] = "done"
    assert call("POST /jobs", {"settings": SETTINGS, "files": files(3)})[0] == 200
    st, out = call("POST /jobs", {"settings": SETTINGS, "files": files(1)})
    assert st == 429 and "24 hours" in out["error"]
    assert call("POST /jobs", {"settings": SETTINGS, "files": files(1)}, sub="someone-else")[0] == 200


def test_signed_out_requests_are_refused(aws):
    """A request with no identity from the authoriser gets a 401, not a crash."""
    out = api.handler(event("GET /jobs", signed_in=False))
    assert out["statusCode"] == 401


# ---- uploading and queueing ------------------------------------------------
def test_submit_queues_a_valid_upload(aws):
    """After the zip arrives, submit checks it and the run joins the queue; a second submit changes nothing."""
    job_id, _ = new_run(aws)
    st, out = call("POST /jobs/{id}/submit", params={"id": job_id})
    assert st == 200 and out["job"]["status"] == "queued" and out["job"]["queued_at"]
    assert aws.jobs.items[job_id]["step_name"] == "wing.step"
    again = call("POST /jobs/{id}/submit", params={"id": job_id})
    assert again[0] == 200 and again[1]["job"]["queued_at"] == out["job"]["queued_at"]


def test_submit_before_the_upload_arrives(aws):
    """If the zip is not in S3 yet the run stays waiting (409), so the browser can retry."""
    st, made = call("POST /jobs", {"settings": SETTINGS, "files": files(1)})
    job_id = made["jobs"][0]["job"]["id"]
    st, out = call("POST /jobs/{id}/submit", params={"id": job_id})
    assert st == 409 and aws.jobs.items[job_id]["status"] == "awaiting_upload"


def test_submit_rejects_a_bad_zip_with_the_reason(aws):
    """A zip with the wrong contents turns the run into 'failed' with a readable message."""
    job_id, key = new_run(aws)
    aws.s3.objects[("up", key)] = make_zip({"a.step": STEP_TEXT, "b.step": STEP_TEXT})
    st, out = call("POST /jobs/{id}/submit", params={"id": job_id})
    assert st == 200 and out["job"]["status"] == "failed" and "2 files" in out["job"]["message"]
    assert call("POST /jobs/{id}/submit", params={"id": job_id})[0] == 409


def test_other_peoples_runs_look_missing(aws):
    """Another user's run id gives a 404 on every route; so does a malformed id."""
    job_id, _ = new_run(aws, sub="owner")
    for route in ("POST /jobs/{id}/submit", "GET /jobs/{id}", "POST /jobs/{id}/cancel"):
        assert call(route, params={"id": job_id}, sub="intruder")[0] == 404
    assert call("GET /jobs/{id}", params={"id": "../../etc"})[0] == 404


# ---- listing and details -----------------------------------------------------
def test_list_shows_my_runs_queue_positions_and_the_worker(aws):
    """The list has only my runs, newest first, with how many runs are ahead of each queued one."""
    ids = []
    for i in range(3):
        job_id, _ = new_run(aws, label=f"w{i}")
        call("POST /jobs/{id}/submit", params={"id": job_id})
        aws.jobs.items[job_id]["created_at"] = f"2026-11-12T10:0{i}:00Z"
        aws.jobs.items[job_id]["queued_at"] = f"2026-11-12T10:0{i}:00Z"
        ids.append(job_id)
    aws.jobs.items[ids[0]]["status"] = "running"
    other, _ = new_run(aws, sub="stranger")
    st, out = call("GET /jobs")
    assert st == 200 and [j["id"] for j in out["jobs"]] == ids[::-1] and other not in {j["id"] for j in out["jobs"]}
    ahead = {j["id"]: j.get("runs_ahead") for j in out["jobs"]}
    assert ahead[ids[1]] == 1 and ahead[ids[2]] == 2 and ahead[ids[0]] is None
    assert out["worker"] == {"online": False, "last_seen_seconds": None}
    assert out["limits"]["max_files"] == 5
    aws.jobs.items["_worker"] = {"job_id": "_worker", "last_seen": cc.utc_now()}
    assert call("GET /jobs")[1]["worker"]["online"] is True
    aws.jobs.items["_worker"]["last_seen"] = "2020-01-01T00:00:00Z"
    assert call("GET /jobs")[1]["worker"]["online"] is False


def test_detail_has_download_links_only_for_safe_names(aws):
    """Finished runs get short-lived links for their files; odd file names are dropped."""
    job_id, _ = new_run(aws)
    aws.jobs.items[job_id].update(status="done", files=[
        {"name": "01_hero.png", "kind": "image", "title": "Hero"}, {"name": "results.zip", "kind": "archive"},
        {"name": "../secret", "kind": "image"}])
    st, out = call("GET /jobs/{id}", params={"id": job_id})
    names = [f["name"] for f in out["job"]["files"]]
    assert st == 200 and names == ["01_hero.png", "results.zip"]
    assert out["job"]["files"][0]["url"].endswith(f"results/{job_id}/01_hero.png?sig=x")
    assert out["job"]["files"][1]["url"].endswith("&dl=1")


# ---- cancelling ----------------------------------------------------------------
def test_cancel_rules(aws):
    """Waiting runs cancel at once, running ones are flagged for the worker, finished ones are refused."""
    waiting, _ = new_run(aws, label="a")
    assert call("POST /jobs/{id}/cancel", params={"id": waiting})[1]["job"]["status"] == "cancelled"
    running, _ = new_run(aws, label="b")
    aws.jobs.items[running]["status"] = "running"
    out = call("POST /jobs/{id}/cancel", params={"id": running})[1]["job"]
    assert out["status"] == "running" and out["cancel_requested"] is True
    assert call("POST /jobs/{id}/cancel", params={"id": waiting})[0] == 409


# ---- access requests -----------------------------------------------------------
def test_access_request_is_saved_and_emailed_once(aws):
    """A request is stored and emailed with the approve command; asking again sends nothing and says the same."""
    body = {"email": "New.Person@Example.com ", "name": "New Person", "organisation": "Team", "message": "Hi"}
    st, first = call("POST /request-access", body, signed_in=False)
    assert st == 200 and aws.requests.items["new.person@example.com"]["status"] == "pending"
    assert len(aws.sns.sent) == 1 and "approve new.person@example.com" in aws.sns.sent[0]["Message"]
    st, second = call("POST /request-access", body, signed_in=False)
    assert st == 200 and second == first and len(aws.sns.sent) == 1


def test_access_request_checks_and_resists_robots(aws):
    """Bad emails and missing names are 400s; the hidden field makes a robot's request vanish quietly."""
    assert call("POST /request-access", {"email": "nope", "name": "Bob"}, signed_in=False)[0] == 400
    assert call("POST /request-access", {"email": "a@b.co", "name": ""}, signed_in=False)[0] == 400
    robot = {"email": "bot@b.co", "name": "Bot", "website": "http://spam"}
    st, _ = call("POST /request-access", robot, signed_in=False)
    assert st == 200 and not aws.requests.items and not aws.sns.sent


def test_access_request_survives_a_broken_email_service(aws):
    """If SNS is down the request is still saved and the visitor still sees success."""
    aws.sns.fail = True
    st, _ = call("POST /request-access", {"email": "a@b.co", "name": "Ann"}, signed_in=False)
    assert st == 200 and "a@b.co" in aws.requests.items


# ---- errors and alerts -----------------------------------------------------------
def test_unknown_route_and_bad_json(aws):
    """Unknown routes are 404; a body that is not a JSON object is 400."""
    assert api.handler({"routeKey": "GET /nothing"})["statusCode"] == 404
    ev = event("POST /jobs")
    ev["body"] = "[1, 2]"
    assert api.handler(ev)["statusCode"] == 400
    ev["body"] = "{oops"
    assert api.handler(ev)["statusCode"] == 400
    ev["body"] = "x" * 60_000
    assert api.handler(ev)["statusCode"] == 413


def test_unexpected_error_is_hidden_from_the_user_but_emailed_once(aws, monkeypatch):
    """A crash gives a plain 500, emails the owner the details, and a second crash within 10 minutes stays quiet."""
    monkeypatch.setitem(api.ROUTES, "GET /jobs", lambda req: 1 / 0)
    out = api.handler(event("GET /jobs"))
    assert out["statusCode"] == 500 and "ZeroDivisionError" not in out["body"]
    assert len(aws.sns.sent) == 1 and "ZeroDivisionError" in aws.sns.sent[0]["Message"]
    api.handler(event("GET /jobs"))
    assert len(aws.sns.sent) == 1


def test_response_headers_and_content_type(aws):
    """Responses are JSON and never cached."""
    out = api.handler(event("GET /jobs"))
    assert out["headers"]["Cache-Control"] == "no-store" and json.loads(out["body"])["jobs"] == []


def test_wings_made_together_queue_in_the_order_they_were_chosen(aws, monkeypatch):
    """Runs created in the same second keep a fixed order (their position in the upload), whatever their ids are."""
    monkeypatch.setattr(cc, "utc_now", lambda: "2026-11-12T10:00:00Z")
    st, out = call("POST /jobs", {"settings": {}, "files": [
        {"filename": f"w{i}.zip", "size": 500, "units": "mm"} for i in range(3)]})
    assert st == 200 and [j["job"]["seq"] for j in out["jobs"]] == [0, 1, 2]
    # give them ids that sort in the opposite order to the way they were chosen
    for old, new in zip([j["job"]["id"] for j in out["jobs"]], ("c" * 32, "b" * 32, "a" * 32)):
        item = aws.jobs.items.pop(old)
        item.update(job_id=new, status="queued")
        aws.jobs.items[new] = item
    st, out = call("GET /jobs")
    ahead = {j["id"]: j["runs_ahead"] for j in out["jobs"]}
    assert ahead == {"c" * 32: 0, "b" * 32: 1, "a" * 32: 2}
    assert call("GET /jobs")[1]["jobs"] == out["jobs"]                 # and the answer does not change between polls
