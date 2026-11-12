"""The real pages in a real browser (Chromium via Playwright), with the real API code behind a pretend network.

What this proves: the pages load under the production Content-Security-Policy, sign-in and token renewal work,
STEP files are zipped in the browser and accepted by the server's own checks, up to five wings go up at once,
progress and results show correctly, and nothing from one account is left on screen for the next.
Skipped automatically where Playwright or its browser is not installed.
"""
import io
import json
import os
import random
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright  # noqa: E402

import api  # noqa: E402
import cfd_common as cc  # noqa: E402
import ui_harness as ui  # noqa: E402
from conftest import FakeS3, FakeSNS, FakeTable, make_zip  # noqa: E402

SHOTS = os.environ.get("UI_SHOTS")           # set to a folder to also save screenshots


def step_bytes(lines=3000, seed=1):
    """A believable STEP file: a header and many lines of numbers (compresses a few times over, not 1000x)."""
    rnd = random.Random(seed)
    rows = ["ISO-10303-21;", "HEADER;", "FILE_DESCRIPTION(('test wing'),'2;1');", "ENDSEC;", "DATA;"]
    for i in range(1, lines + 1):
        x, y, z = (rnd.uniform(0, 900) for _ in range(3))
        rows.append(f"#{i}=CARTESIAN_POINT('',({x:.6f},{y:.6f},{z:.6f}));")
    return ("\n".join(rows + ["ENDSEC;", "END-ISO-10303-21;", ""])).encode()


@pytest.fixture(scope="module")
def site():
    """The pages, served locally with the production security headers."""
    server = ui.SiteServer()
    yield server
    server.close()


@pytest.fixture(scope="module")
def browser():
    """One headless Chromium for the whole file."""
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:                                    # noqa: BLE001 - no browser installed
            pytest.skip(f"Chromium is not available: {exc}")
        yield b
        b.close()


@pytest.fixture(scope="module")
def assets(tmp_path_factory):
    """A small picture and (if ffmpeg exists) a small video for the results pages."""
    from PIL import Image
    folder = tmp_path_factory.mktemp("assets")
    buf = io.BytesIO()
    Image.new("RGB", (480, 300), (40, 110, 190)).save(buf, "PNG")
    mp4 = None
    if shutil.which("ffmpeg"):
        out = folder / "o.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x200:rate=10",
                        "-pix_fmt", "yuv420p", str(out)], check=True)
        mp4 = out.read_bytes()
    return buf.getvalue(), mp4


class Session:
    """One browser page wired to a fresh pretend cloud, plus what the page printed to its console."""

    def __init__(self, browser, site, be, assets, viewport=None, dark=False):
        """Open a page whose network goes to a new Cloud."""
        self.be, self.site = be, site
        self.cloud = ui.Cloud(be, site.url, *assets)
        self.context = browser.new_context(viewport=viewport or {"width": 1100, "height": 900},
                                           color_scheme="dark" if dark else "light")
        self.context.route("**/*", self.cloud.handle)
        self.page = self.context.new_page()
        self.problems = []
        self.page.on("console", lambda m: self.problems.append(m.text) if m.type in ("error", "warning") else None)
        self.page.on("pageerror", lambda e: self.problems.append(f"page error: {e}"))

    def open(self):
        """Load the site."""
        self.page.goto(self.site.url + "/index.html")
        return self.page

    def sign_in(self, email="alice@example.com", password=ui.PASSWORD):
        """Fill the sign-in form and wait for the signed-in screen."""
        p = self.open() if self.page.url == "about:blank" else self.page
        p.fill("#signin-email", email)
        p.fill("#signin-password", password)
        p.click("#form-signin button[type=submit]")
        expect(p.locator("#view-app")).to_be_visible()
        return p

    def shot(self, name):
        """Save a screenshot if UI_SHOTS is set."""
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(Path(SHOTS) / f"{name}.png"), full_page=True)

    def csp_problems(self):
        """Console messages that mean the security policy blocked something, or the page threw an error."""
        return [m for m in self.problems if "Content Security Policy" in m or "Refused" in m or "page error" in m]

    def close(self):
        """Close the browser context."""
        self.context.close()


@pytest.fixture
def backends(env, monkeypatch):
    """Fresh in-memory AWS wired into the API module (same as the aws fixture)."""
    be = api.Backends(FakeTable(), FakeTable(key="email", indexes={}), FakeS3(), FakeSNS())
    monkeypatch.setattr(api, "_backends", be)
    return be


@pytest.fixture
def make_session(browser, site, backends, assets):
    """Open sessions (several are allowed); all are closed afterwards and checked for security-policy errors."""
    opened = []

    def make(**kw):
        s = Session(browser, site, backends, assets, **kw)
        opened.append(s)
        return s

    yield make
    for s in opened:
        bad = s.csp_problems()
        s.close()
        assert not bad, bad


@pytest.fixture
def wing_files(tmp_path):
    """Three wings on disk: two plain STEP files (one .stp) and a ready-made zip."""
    paths = {"front": tmp_path / "front_wing.step", "rear": tmp_path / "Rear Wing v2.STP",
             "zipped": tmp_path / "side_plate.zip", "text": tmp_path / "notes.txt"}
    paths["front"].write_bytes(step_bytes(4000, 1))
    paths["rear"].write_bytes(step_bytes(3000, 2))
    paths["zipped"].write_bytes(make_zip({"side_plate.step": step_bytes(1500, 3)}))
    paths["text"].write_text("not a wing")
    return paths


def queue_via_api(be, sub="sub-alice", email="alice@example.com", n=1, label="wing"):
    """Create runs straight through the API (as if made earlier), each with a valid zip uploaded and queued."""
    ids = []
    for i in range(n):
        status, out = (lambda r: (r["statusCode"], json.loads(r["body"])))(api.handler({
            "routeKey": "POST /jobs", "pathParameters": {}, "requestContext": {
                "http": {"sourceIp": "1.1.1.1"},
                "authorizer": {"jwt": {"claims": {"sub": sub, "email": email}}}},
            "body": json.dumps({"settings": {"solver": "SA", "preset": "quick", "speed": 20, "ground": "moving"},
                                "files": [{"filename": f"{label}{i}.zip", "size": 900, "units": "mm",
                                           "label": f"{label} {i}"}]})}))
        assert status == 200, out
        job = out["jobs"][0]
        be.s3.objects[("up", job["upload"]["fields"]["key"])] = make_zip({"a.step": step_bytes(50, i)})
        cc.update_fields(be.jobs, {"job_id": job["job"]["id"]},
                         {"status": cc.QUEUED, "queued_at": cc.utc_now(), "created_at": f"2026-01-01T00:00:{i:02d}Z"})
        ids.append(job["job"]["id"])
    return ids


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_request_access_emails_the_owner_and_ignores_robots(make_session, backends):
    """The form stores the request and alerts the owner; the hidden field catches robots without telling them."""
    s = make_session()
    p = s.open()
    s.shot("1_signin")
    p.click("#go-access")
    expect(p.locator("#view-access")).to_be_visible()
    p.fill("#access-name", "Jo Racer")
    p.fill("#access-email", "jo@team.example")
    p.fill("#access-org", "Uni Racing")
    p.fill("#access-message", "Front wing sweeps for next season.")
    s.shot("2_request")
    p.click("#form-access button[type=submit]")
    expect(p.locator("#message")).to_contain_text("Thank you")
    expect(p.locator("#view-signin")).to_be_visible()
    assert backends.requests.items["jo@team.example"]["status"] == "pending"
    assert len(backends.sns.sent) == 1 and "approve jo@team.example" in backends.sns.sent[0]["Message"]

    p.click("#go-access")
    p.fill("#access-name", "Bot")
    p.fill("#access-email", "bot@spam.example")
    p.evaluate("document.getElementById('access-extra').value = 'http://spam'")
    p.click("#form-access button[type=submit]")
    expect(p.locator("#message")).to_contain_text("Thank you")
    assert "bot@spam.example" not in backends.requests.items and len(backends.sns.sent) == 1

    p.click("#go-access")
    p.fill("#access-name", "J")
    p.fill("#access-email", "not-an-email")
    p.click("#form-access button[type=submit]")
    expect(p.locator("#message")).to_contain_text("name")


def test_wrong_password_and_sign_in(make_session):
    """A wrong password gives the generic message; the right one reaches the signed-in screen."""
    s = make_session()
    p = s.open()
    p.fill("#signin-email", "alice@example.com")
    p.fill("#signin-password", "nope")
    p.click("#form-signin button[type=submit]")
    expect(p.locator("#message")).to_contain_text("Incorrect email or password")
    s.sign_in()
    expect(p.locator("#app-email")).to_have_text("alice@example.com")
    expect(p.locator("#card")).to_have_class("card wide")
    expect(p.locator("#runs-note")).to_contain_text("No runs yet")


def test_upload_five_mixed_files_end_to_end(make_session, backends, wing_files, tmp_path):
    """Three wings (two STEP files, one zip) go up together, are zipped in the browser, accepted and queued."""
    s = make_session()
    worker = ui.worker_online
    worker(backends)
    p = s.sign_in()
    expect(p.locator("#worker-banner")).to_contain_text("worker is online")
    p.set_input_files("#run-files", [str(wing_files[k]) for k in ("front", "rear", "zipped", "text")])
    expect(p.locator(".wing")).to_have_count(3)
    expect(p.locator("#upload-status")).to_contain_text("notes.txt")
    assert "will be zipped" in p.locator(".wing").nth(0).inner_text()
    assert "already a zip" in p.locator(".wing").nth(2).inner_text()

    # nothing is sent until every wing has its units
    p.click("#runs-submit")
    expect(p.locator(".wing .problem").first).to_contain_text("Choose the units")
    assert not backends.jobs.items.keys() - {"_worker"}

    p.locator(".wing").nth(0).locator("select").select_option("mm")
    assert [p.locator(".wing select").nth(i).input_value() for i in range(3)] == ["mm", "mm", "mm"]
    p.locator(".wing").nth(1).locator("select").select_option("in")
    p.locator(".wing").nth(0).locator('input[type="text"]').fill("Front wing A")
    p.locator(".wing").nth(1).locator('input[type="checkbox"]').check()
    p.select_option("#set-solver", "SST")
    p.select_option("#set-preset", "standard")
    p.fill("#set-speed", "35")
    p.select_option("#set-ground", "fixed")
    s.shot("3_form")
    p.click("#runs-submit")
    expect(p.locator("#upload-status")).to_contain_text("3 runs are queued", timeout=30000)
    expect(p.locator(".run")).to_have_count(3)
    s.shot("4_queued")

    jobs = {j["label"]: j for j in backends.jobs.items.values() if j["job_id"] != "_worker"}
    assert set(jobs) == {"Front wing A", "Rear Wing v2", "side plate"}
    assert all(j["status"] == cc.QUEUED and j["user_sub"] == "sub-alice" for j in jobs.values())
    front, rear = jobs["Front wing A"], jobs["Rear Wing v2"]
    assert front["options"] == {"units": "mm", "half_model": False, "solver": "SST", "preset": "standard",
                                "speed": 35, "ground": "fixed"}
    assert rear["options"]["units"] == "in" and rear["options"]["half_model"] is True
    assert front["filename"] == "front_wing.step" and rear["filename"] == "Rear Wing v2.STP"

    # what arrived is a valid zip holding exactly the original bytes, under a plain name
    for label, original in (("Front wing A", wing_files["front"]), ("Rear Wing v2", wing_files["rear"])):
        data = backends.s3.objects[("up", jobs[label]["upload_key"])]
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            (name,) = zf.namelist()
            assert zf.read(name) == original.read_bytes() and zf.testzip() is None
        assert name in ("front_wing.step", "Rear_Wing_v2.stp")
        assert len(data) < original.stat().st_size / 2                    # actually compressed
    assert all(order[-1] == "file" for order in s.cloud.upload_orders) and len(s.cloud.upload_orders) == 3
    assert jobs["side plate"]["step_name"] == "side_plate.step"
    assert p.locator(".wing").count() == 0 and p.locator("#set-solver").input_value() == "SST"   # settings stay


def test_five_wing_limit_and_form_checks(make_session, backends, wing_files, tmp_path):
    """A sixth file is refused with a reason; bad speed, a fake STEP file and an oversize zip are caught."""
    s = make_session()
    p = s.sign_in()
    many = []
    for i in range(6):
        f = tmp_path / f"w{i}.step"
        f.write_bytes(step_bytes(20, i))
        many.append(str(f))
    p.set_input_files("#run-files", many)
    expect(p.locator(".wing")).to_have_count(5)
    expect(p.locator("#upload-status")).to_contain_text("Only 5 wings")
    expect(p.locator("#run-files")).to_be_disabled()

    p.locator(".wing").nth(0).locator("select").select_option("m")
    p.fill("#set-speed", "500")
    p.click("#runs-submit")
    expect(p.locator("#upload-status")).to_contain_text("Speed must be between 5 and 100")
    p.fill("#set-speed", "30")
    for _ in range(4):
        p.locator(".wing").nth(1).get_by_role("button", name="Remove").click()
    expect(p.locator(".wing")).to_have_count(1)
    fake = tmp_path / "fake.step"
    fake.write_text("solid not a step file\nfacet normal 0 0 1")
    p.locator(".wing").nth(0).get_by_role("button", name="Remove").click()
    p.set_input_files("#run-files", [str(fake)])
    p.locator(".wing").nth(0).locator("select").select_option("mm")
    p.click("#runs-submit")
    expect(p.locator(".wing .problem")).to_contain_text("does not look like a STEP file")
    assert not backends.jobs.items.keys() - {"_worker"}


def test_a_bad_zip_is_rejected_with_the_reason_and_others_still_go(make_session, backends, tmp_path):
    """A zip with two files fails the server's check; the good wing in the same upload is still queued."""
    s = make_session()
    p = s.sign_in()
    good, bad = tmp_path / "good.step", tmp_path / "two.zip"
    good.write_bytes(step_bytes(100, 1))
    bad.write_bytes(make_zip({"a.step": step_bytes(10), "b.step": step_bytes(10, 2)}))
    p.set_input_files("#run-files", [str(good), str(bad)])
    p.locator(".wing").nth(0).locator("select").select_option("mm")
    p.click("#runs-submit")
    expect(p.locator("#upload-status")).to_contain_text("1 run is queued", timeout=30000)
    expect(p.locator("#upload-status")).to_contain_text("1 wing could not be uploaded")
    expect(p.locator(".wing .problem")).to_contain_text("holds 2 files")
    by_label = {j["label"]: j["status"] for j in backends.jobs.items.values() if j["job_id"] != "_worker"}
    assert by_label == {"good": cc.QUEUED, "two": cc.FAILED}               # the server records why it was refused
    expect(p.locator(".run")).to_have_count(2)
    expect(p.locator('.run[data-status="failed"]')).to_contain_text("holds 2 files")


def test_a_failed_upload_is_cancelled_and_reported(make_session, backends, tmp_path):
    """If S3 refuses the upload the run is cancelled and the reason stays under the wing."""
    s = make_session()
    p = s.sign_in()
    s.cloud.upload_error = "AccessDenied"
    f = tmp_path / "a.step"
    f.write_bytes(step_bytes(100))
    p.set_input_files("#run-files", [str(f)])
    p.locator(".wing").nth(0).locator("select").select_option("mm")
    p.click("#runs-submit")
    expect(p.locator(".wing .problem")).to_contain_text("refused", timeout=20000)
    assert [j["status"] for j in backends.jobs.items.values() if j["job_id"] != "_worker"] == [cc.CANCELLED]
    s.problems.clear()                       # the browser logs the refused request itself; that is expected here


def test_progress_is_followed_live_and_results_are_shown(make_session, backends, assets):
    """Queue position, stage checklist, iteration counts, then numbers, pictures, video and downloads."""
    s = make_session()
    ids = queue_via_api(backends, n=2, label="wing")
    ui.worker_online(backends)
    p = s.sign_in()
    runs = p.locator(".run")
    expect(runs).to_have_count(2)
    expect(p.locator('.run:has-text("wing 0") .now')).to_contain_text("Next in line")
    expect(p.locator('.run:has-text("wing 1") .now')).to_contain_text("1 run ahead")
    s.shot("5_queue")

    first = ids[0]
    ui.worker_update(backends, first, status=cc.RUNNING, stage="Meshing", started_at=cc.utc_now(),
                     stages={"inspect": {"state": "done", "seconds": 3.2}, "case": {"state": "done", "seconds": 1.1},
                             "mesh": {"state": "running", "seconds": None}}, progress={"cells": 1420000})
    # the page notices by itself (it polls every 8 seconds while anything is unfinished)
    expect(p.locator('.run[data-status="running"] .now')).to_contain_text("Meshing", timeout=20000)
    expect(p.locator('.run[data-status="running"] .now')).to_contain_text("1.42 million cells")
    steps = p.locator('.run[data-status="running"] .steps li')
    assert [steps.nth(i).get_attribute("data-state") for i in range(7)] == \
        ["done", "done", "running", "pending", "pending", "pending", "pending"]
    s.shot("6_running")

    ui.worker_update(backends, first, stage="Solving", progress={"cells": 1420000, "iterations": 340},
                     stages={"inspect": {"state": "done", "seconds": 3.2}, "case": {"state": "done", "seconds": 1.1},
                             "mesh": {"state": "done", "seconds": 61}, "mesh gates": {"state": "done", "seconds": 2},
                             "solve": {"state": "running", "seconds": None}})
    p.click("#refresh")
    expect(p.locator('.run[data-status="running"] .now')).to_contain_text("340 iterations so far")

    files = [{"name": "01_hero.png", "kind": "image", "title": "Streamlines", "size": 1000},
             {"name": "02_side_slice.png", "kind": "image", "title": "Side view, speed", "size": 1000},
             {"name": "orbit.mp4", "kind": "video", "title": "360-degree video", "size": 2000},
             {"name": "summary.md", "kind": "report", "title": "summary", "size": 300},
             {"name": "results.zip", "kind": "archive", "title": "results", "size": 5000}]
    done_stages = {k: {"state": "done", "seconds": 5} for k, _ in [("inspect", 0), ("case", 0), ("mesh", 0),
                                                                    ("mesh gates", 0), ("solve", 0), ("finish", 0),
                                                                    ("render", 0)]}
    ui.worker_update(backends, first, status=cc.DONE, stage="Finished", finished_at=cc.utc_now(), stages=done_stages,
                     message="The run finished.", files=files,
                     summary={"result": "PASS", "cd": 0.4123, "cl": -1.2345, "iterations": 1500, "cells": 1420000,
                              "aref_m2": 0.25, "s_cd": 0.103075, "s_cl": -0.308625, "solver": "SA",
                              "gates": [{"code": "G1", "level": "WARN", "message": "Residuals oscillate slightly."}]})
    p.click("#refresh")
    done = p.locator('.run[data-status="done"]')
    expect(done).to_have_count(1)
    expect(done.locator(".numbers")).to_contain_text("0.4123")
    expect(done.locator(".numbers")).to_contain_text("-1.2345")
    expect(done.locator(".numbers")).to_contain_text("PASS")
    expect(done).to_contain_text("Residuals oscillate slightly")
    expect(done).to_contain_text("Negative lift means downforce")
    assert not done.locator("img").count()                                 # nothing is fetched until it is opened
    done.locator("summary", has_text="Pictures, video and downloads").click()
    expect(done.locator("img")).to_have_count(2)
    expect(done.locator("img").first).to_have_js_property("complete", True)
    assert done.locator("img").first.evaluate("e => e.naturalWidth") == 480
    expect(done.locator("video")).to_have_count(1)
    assert "orbit.mp4" in done.locator("video").get_attribute("src")
    expect(done.get_by_role("link", name="summary")).to_be_visible()
    expect(done.get_by_role("link", name="results")).to_be_visible()
    s.shot("7_results")
    # a later poll must leave the open panel (and any playing video) alone
    done.locator("video").evaluate("e => { e.dataset.same = '1'; }")
    p.click("#refresh")
    p.wait_for_timeout(500)
    assert done.locator("video").get_attribute("data-same") == "1"
    expect(p.locator('.run[data-status="queued"]')).to_have_count(1)


def test_failures_stops_cancelling_and_offline_worker(make_session, backends):
    """Failed and stopped runs say why; a queued run can be cancelled in two clicks; an offline worker is flagged."""
    s = make_session()
    a, b, c = queue_via_api(backends, n=3, label="r")
    ui.worker_update(backends, a, status=cc.FAILED, stage="Failed", finished_at=cc.utc_now(),
                     message="OpenFOAM stopped with an error in the mesh step.",
                     stages={"inspect": {"state": "done", "seconds": 2}, "case": {"state": "done", "seconds": 1},
                             "mesh": {"state": "failed", "seconds": 9}})
    ui.worker_update(backends, b, status=cc.STOPPED, stage="Stopped by a quality check", finished_at=cc.utc_now(),
                     message="The geometry check did not pass: the wing is not watertight.")
    p = s.sign_in()
    expect(p.locator("#worker-banner")).to_contain_text("offline")
    expect(p.locator('.run[data-status="failed"]')).to_contain_text("error in the mesh step")
    expect(p.locator('.run[data-status="stopped"]')).to_contain_text("not watertight")
    queued = p.locator('.run[data-status="queued"]')
    expect(queued).to_contain_text("worker is offline")
    s.shot("8_problems")
    button = queued.get_by_role("button", name="Cancel this run")
    button.click()
    expect(queued.get_by_role("button", name="Click again to cancel")).to_be_visible()
    assert backends.jobs.items[c]["status"] == cc.QUEUED
    queued.get_by_role("button", name="Click again to cancel").click()
    expect(p.locator('.run[data-status="cancelled"]')).to_have_count(1)
    assert backends.jobs.items[c]["status"] == cc.CANCELLED


def test_sign_out_clears_the_screen_and_accounts_do_not_mix(make_session, backends):
    """After sign-out nothing of the first account stays in the page; the second account sees only its own runs."""
    s = make_session()
    queue_via_api(backends, sub="sub-alice", email="alice@example.com", n=2, label="alice")
    queue_via_api(backends, sub="sub-bob", email="bob@example.com", n=1, label="bob")
    p = s.sign_in("alice@example.com")
    expect(p.locator(".run")).to_have_count(2)
    p.click("#signout")
    expect(p.locator("#view-signin")).to_be_visible()
    assert p.locator(".run").count() == 0 and "alice" not in p.inner_text("#view-app")
    assert p.evaluate("sessionStorage.getItem('cfd.tokens')") is None
    s.sign_in("bob@example.com")
    expect(p.locator(".run")).to_have_count(1)
    expect(p.locator(".run")).to_contain_text("bob 0")


def test_tokens_are_renewed_and_a_dead_session_returns_to_sign_in(make_session, backends):
    """An expired ID token is renewed with the refresh token; when the server rejects the session the page signs out."""
    s = make_session()
    queue_via_api(backends, n=1)
    s.cloud.token_seconds = 30                                 # shorter than the one-minute safety margin
    p = s.sign_in()
    expect(p.locator(".run")).to_have_count(1)
    before = s.cloud.refreshes
    p.click("#refresh")
    p.wait_for_timeout(500)
    assert s.cloud.refreshes > before                          # the token was renewed before the call
    s.cloud.force_401 = True
    p.click("#refresh")
    expect(p.locator("#view-signin")).to_be_visible()
    expect(p.locator("#message")).to_contain_text("session has ended")
    assert p.locator(".run").count() == 0
    s.problems.clear()                                         # the browser logs the 401 itself


def test_layouts_on_a_phone_and_in_dark_mode(make_session, backends, assets):
    """No sideways scrolling on a 375 px screen, in light or dark mode; screenshots are saved when asked."""
    ids = queue_via_api(backends, n=2)
    ui.worker_online(backends)
    ui.worker_update(backends, ids[0], status=cc.RUNNING, stage="Solving", started_at=cc.utc_now(),
                     stages={"inspect": {"state": "done", "seconds": 3},
                             "solve": {"state": "running", "seconds": None}},
                     progress={"cells": 900000, "iterations": 120})
    for name, kw in (("phone_light", {"viewport": {"width": 375, "height": 800}}),
                     ("phone_dark", {"viewport": {"width": 375, "height": 800}, "dark": True}),
                     ("desktop_dark", {"dark": True})):
        s = make_session(**kw)
        p = s.sign_in()
        p.locator(".run").first.wait_for()
        p.set_input_files("#run-files", [])
        s.shot(f"9_{name}")
        assert p.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), name
