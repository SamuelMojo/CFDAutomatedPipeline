// The signed-in screen of the CFD pipeline site: start runs (1 to 5 wings at once), watch them, read the results.
//
// How it fits together:
//   - app.js signs people in and sends "cfd:signedin" / "cfd:signedout"; window.CFD_AUTH.idToken() gives a token.
//   - Starting runs: the chosen STEP files are zipped here (zip.js), the API creates one run per wing and returns a
//     signed upload form for each, the zip goes straight to S3, then the API is told to check it and queue it.
//   - Watching runs: the list is fetched every few seconds while anything is unfinished (more slowly otherwise,
//     and not at all while the tab is hidden). A run's card is only rebuilt when its data changed, so a video that
//     is playing is never interrupted.
// No innerHTML and no inline handlers: the page's Content-Security-Policy forbids them.
(() => {
  "use strict";

  const cfg = window.CFD_CONFIG;
  const ZIP = window.CFD_ZIP;
  const $ = (id) => document.getElementById(id);

  // ---- constants -----------------------------------------------------------------------------------------

  // The checklist shown for each run: run_job.py's stage names, in order, with the wording for the page.
  const STEPS = [
    ["inspect", "Check the geometry"],
    ["case", "Set up the case"],
    ["mesh", "Build the mesh"],
    ["mesh gates", "Check the mesh"],
    ["solve", "Solve the flow"],
    ["finish", "Finish the case"],
    ["render", "Make pictures and video"],
  ];
  const ACTIVE = ["awaiting_upload", "queued", "running"];
  const STATUS_WORDS = {
    awaiting_upload: "Uploading", queued: "Queued", running: "Running", done: "Finished",
    stopped: "Stopped by a check", failed: "Failed", cancelled: "Cancelled",
  };
  const SOLVER_NAMES = { SA: "Spalart-Allmaras", SST: "k-omega SST" };
  const PRESET_NAMES = { quick: "Preview quality", standard: "Standard quality" };   // "quick" is the stored key
  const GROUND_NAMES = { moving: "moving ground", fixed: "fixed floor" };
  const UNIT_CHOICES = [["mm", "millimetres (mm)"], ["cm", "centimetres (cm)"], ["m", "metres (m)"], ["in", "inches (in)"]];
  const POLL_ACTIVE_MS = 8000;       // while a run is unfinished
  const POLL_IDLE_MS = 30000;        // when everything is finished
  const POLL_HIDDEN_MS = 60000;      // while the tab is in the background
  const LINK_REFRESH_MS = 10 * 60 * 1000;   // result links last 15 minutes; renew them after 10
  const PREFS_KEY = "cfd.prefs";

  // ---- state ---------------------------------------------------------------------------------------------

  let limits = { max_files: 5, max_upload_mb: 100, max_step_mb: 100, max_active: 10, speed_min: 5, speed_max: 100 };
  let wings = [];                    // files chosen but not yet uploaded: {id, file, label, units, half, zip, problem}
  let nextWingId = 1;
  let uploading = false;
  let upload = null;                 // the XHR in flight, so signing out can stop it
  let jobs = [];                     // the latest list from the server
  let worker = null;                 // {online, last_seen_seconds}
  let signedIn = false;
  let loading = false;
  let timer = null;
  const cards = new Map();           // run id -> {sig, el}
  const opened = new Map();          // run id -> {at, urls} for result panels that are open

  // ---- small helpers -------------------------------------------------------------------------------------

  // Build an element: h("p", {class: "hint", text: "Hi"}, child, child). Strings become text, never markup.
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) el.append(kid);
    return el;
  }

  function fmtBytes(n) { return n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} kB`; }
  function fmtDuration(seconds) {
    const s = Math.max(0, Math.round(seconds));
    if (s < 90) return `${s} s`;
    if (s < 5400) return `${Math.round(s / 60)} min`;
    const m = Math.round(s / 60);
    return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
  }
  function secondsSince(iso) { const t = Date.parse(iso || ""); return Number.isFinite(t) ? (Date.now() - t) / 1000 : null; }
  function secondsBetween(a, b) {
    const x = Date.parse(a || ""); const y = Date.parse(b || "");
    return Number.isFinite(x) && Number.isFinite(y) ? (y - x) / 1000 : null;
  }
  function fmtTime(iso) {
    const t = Date.parse(iso || "");
    return Number.isFinite(t) ? new Date(t).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "";
  }
  function num(x, digits) { return typeof x === "number" && Number.isFinite(x) ? x.toFixed(digits) : "-"; }
  function fmtCells(n) { return n >= 1e6 ? `${(n / 1e6).toFixed(2)} million cells` : `${Math.round(n).toLocaleString()} cells`; }
  function stem(name) { return name.replace(/\.(zip|step|stp)$/i, "").replace(/[_-]+/g, " ").trim().slice(0, 60); }
  function plural(n, one, many) { return `${n} ${n === 1 ? one : many}`; }

  // The sentence to show for an error from the API, a failed upload or a lost connection.
  function describe(err) {
    if (err && err.shown) return err.message;
    if (err instanceof TypeError) return "Could not reach the server. Check your connection.";
    return "Something went wrong. Please try again.";
  }

  function loadPrefs() {
    try { return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") || {}; } catch (_) { return {}; }
  }
  function savePrefs(prefs) {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch (_) { /* private mode: not needed */ }
  }

  // ---- talking to the API --------------------------------------------------------------------------------

  // One call to the site's API with the signed-in token. Errors carry .shown (safe to display) and .status.
  async function api(method, path, body) {
    const token = await window.CFD_AUTH.idToken();
    const res = await fetch(cfg.apiUrl + path, {
      method,
      headers: { Authorization: `Bearer ${token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) throw window.CFD_AUTH.sessionEnded();
    if (!res.ok) {
      const err = new Error(res.status === 429 ? "Too many requests. Wait a moment and try again."
        : data.error || `The server could not do that (code ${res.status}).`);
      err.shown = true;
      err.status = res.status;
      throw err;
    }
    return data;
  }

  // Send one zip to S3 with the signed form from the API. The file must be the last field of the form.
  function postToS3(form, blob, onProgress) {
    return new Promise((resolve, reject) => {
      const data = new FormData();
      Object.entries(form.fields).forEach(([k, v]) => data.append(k, v));
      data.append("file", blob, "wing.zip");
      const xhr = new XMLHttpRequest();
      upload = xhr;
      xhr.open("POST", form.url);
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded); };
      xhr.onload = () => {
        upload = null;
        if (xhr.status >= 200 && xhr.status < 300) { resolve(); return; }
        const code = (/<Code>([^<]+)<\/Code>/.exec(xhr.responseText) || [])[1] || "";
        const why = code === "EntityTooLarge" ? "The file is larger than the upload limit."
          : /Expired|Policy/.test(code + xhr.responseText) ? "The upload form expired. Try again."
            : `The upload was refused (${code || xhr.status}).`;
        const err = new Error(why);
        err.shown = true;
        reject(err);
      };
      xhr.onerror = () => { upload = null; reject(new TypeError("network")); };
      xhr.onabort = () => { upload = null; const err = new Error("The upload was stopped."); err.shown = true; reject(err); };
      xhr.send(data);
    });
  }

  // ---- the upload form -----------------------------------------------------------------------------------

  // A line under the form: kind "" (plain) or "error".
  function say(text, kind) {
    const el = $("upload-status");
    el.textContent = text || "";
    el.className = "status-line" + (kind ? " " + kind : "");
  }

  function setProgress(fraction) {
    const bar = $("upload-progress");
    bar.hidden = fraction === null;
    if (fraction !== null) bar.value = Math.min(Math.max(fraction, 0), 1);
  }

  // Redraw the list of chosen wings and the submit button. The wings array is the single source of truth.
  function renderWings() {
    const list = $("wing-list");
    list.replaceChildren(...wings.map((w) => {
      const id = `wing-${w.id}`;
      const note = w.zip ? `${fmtBytes(w.file.size)}, already a zip`
        : `${fmtBytes(w.file.size)}, will be zipped here before upload`;
      const units = h("select", { id: `${id}-units`, required: true, disabled: uploading,
        onchange: (e) => setUnits(w, e.target.value) },
        h("option", { value: "", text: "Choose..." }),
        UNIT_CHOICES.map(([v, t]) => h("option", { value: v, text: t, selected: w.units === v })));
      return h("li", { class: "wing" },
        h("div", { class: "wing-top" },
          h("strong", { class: "wing-file", text: w.file.name }),
          h("span", { class: "note", text: note }),
          h("button", { type: "button", class: "link", text: "Remove", disabled: uploading,
            onclick: () => { wings = wings.filter((x) => x !== w); renderWings(); } })),
        h("div", { class: "grid" },
          h("div", {},
            h("label", { for: `${id}-label`, text: "Name for this run" }),
            h("input", { id: `${id}-label`, type: "text", maxlength: 60, value: w.label, disabled: uploading,
              oninput: (e) => { w.label = e.target.value; } })),
          h("div", {}, h("label", { for: `${id}-units`, text: "Units the model is drawn in" }), units)),
        h("label", { class: "check" },
          h("input", { type: "checkbox", checked: w.half, disabled: uploading, onchange: (e) => { w.half = e.target.checked; } }),
          "Mesh half the domain (only for a wing symmetric about Y = 0; faster)"),
        h("p", { class: "problem", role: "alert", text: w.problem || "" }));
    }));
    const n = wings.length;
    const button = $("runs-submit");
    button.textContent = uploading ? "Working..." : n ? `Upload and queue ${plural(n, "run", "runs")}` : "Upload and queue";
    button.disabled = uploading || n === 0;
    $("run-files").disabled = uploading || n >= limits.max_files;
  }

  // Picking units for one wing fills in the wings that have none yet (a team usually draws everything in mm).
  function setUnits(w, value) {
    const blanks = wings.filter((x) => x !== w && !x.units);
    w.units = value;
    if (value) blanks.forEach((x) => { x.units = value; });
    wings.forEach((x) => { if (x.units) x.problem = ""; });
    if (wings.every((x) => x.units)) say("");
    renderWings();
    const again = $(`wing-${w.id}-units`);                    // the list was redrawn; keep the keyboard where it was
    if (again) again.focus();
  }

  // Add chosen or dropped files to the list, skipping anything that cannot be used and saying why.
  function addFiles(files) {
    const notes = [];
    for (const file of files) {
      const ext = (file.name.split(".").pop() || "").toLowerCase();
      if (!["step", "stp", "zip"].includes(ext)) { notes.push(`${file.name}: only .step, .stp or .zip files can be used.`); continue; }
      if (file.size === 0) { notes.push(`${file.name} is empty.`); continue; }
      if (wings.some((w) => w.file.name === file.name && w.file.size === file.size
        && w.file.lastModified === file.lastModified)) continue;
      if (wings.length >= limits.max_files) {
        notes.push(`Only ${limits.max_files} wings can be uploaded at once; ${file.name} was not added.`);
        continue;
      }
      const known = wings.find((w) => w.units);
      wings.push({ id: nextWingId++, file, label: stem(file.name), units: known ? known.units : "",
        half: false, zip: ext === "zip", problem: "" });
    }
    renderWings();
    say(notes.join(" "), notes.length ? "error" : "");
  }

  function readSettings() {
    return {
      solver: $("set-solver").value,
      preset: $("set-preset").value,
      speed: Number($("set-speed").value),
      ground: $("set-ground").value,
    };
  }

  // Check the form before anything is sent. Returns an error sentence, or "" if all is well; per-wing problems
  // are shown under the wing itself.
  function formProblem(settings) {
    if (!wings.length) return "Choose at least one STEP file.";
    if (!Number.isFinite(settings.speed) || settings.speed < limits.speed_min || settings.speed > limits.speed_max) {
      return `Speed must be between ${limits.speed_min} and ${limits.speed_max} m/s.`;
    }
    let first = null;
    for (const w of wings) {
      w.problem = "";
      if (!w.units) w.problem = "Choose the units this model is drawn in.";
      else if (!w.zip && w.file.size > limits.max_step_mb * 1e6) {
        w.problem = `This file is ${fmtBytes(w.file.size)}; the limit is ${limits.max_step_mb} MB.`;
      } else if (w.zip && w.file.size > limits.max_upload_mb * 1e6) {
        w.problem = `This zip is ${fmtBytes(w.file.size)}; the limit is ${limits.max_upload_mb} MB.`;
      }
      if (w.problem && !first) first = w;
    }
    if (first) {
      renderWings();
      const field = $(`wing-${first.id}-units`);
      if (field && !first.units) field.focus();
      return "Fix the problems marked below, then try again.";
    }
    return "";
  }

  // Zip every chosen file (or pass a ready zip through) and check it is small enough. Throws {wing, text}.
  async function prepare() {
    const out = [];
    const total = wings.reduce((a, w) => a + w.file.size, 0) || 1;
    let done = 0;
    for (let i = 0; i < wings.length; i++) {
      const w = wings[i];
      say(`Preparing ${w.file.name} (${i + 1} of ${wings.length})...`);
      let blob;
      if (w.zip) {
        // A ready-made zip is copied into memory first: the upload then cannot be cut short if the file on disk
        // changes or the file picker has been cleared. (It is limited to the upload size, checked before this.)
        const bytes = new Uint8Array(await w.file.arrayBuffer());
        if (bytes.length < 4 || bytes[0] !== 0x50 || bytes[1] !== 0x4b) {
          throw { wing: w, text: "This does not look like a zip file." };
        }
        blob = new Blob([bytes], { type: "application/zip" });
      } else {
        if (!(await ZIP.looksLikeStep(w.file))) {
          throw { wing: w, text: "This does not look like a STEP file (it should start with ISO-10303-21)." };
        }
        blob = await ZIP.zipFile(w.file, ZIP.entryName(w.file.name), (f) => setProgress((done + f * w.file.size) / total));
      }
      done += w.file.size;
      setProgress(done / total);
      if (blob.size > limits.max_upload_mb * 1e6) {
        throw { wing: w, text: `After zipping this is ${fmtBytes(blob.size)}; the limit is ${limits.max_upload_mb} MB.` };
      }
      out.push({ w, blob });
    }
    return out;
  }

  // The whole "Upload and queue" action.
  async function startRuns() {
    if (uploading) return;
    const settings = readSettings();
    const problem = formProblem(settings);
    if (problem) { say(problem, "error"); return; }
    uploading = true;
    renderWings();
    document.querySelectorAll("#batch-settings select, #batch-settings input").forEach((el) => { el.disabled = true; });
    try {
      const prepared = await prepare();
      say("Creating the runs...");
      setProgress(0);
      const created = await api("POST", "/jobs", {
        settings,
        files: prepared.map(({ w, blob }) => ({
          filename: w.file.name, size: blob.size, label: w.label.trim(), units: w.units, half_model: w.half,
        })),
      });
      await sendAll(created.jobs, prepared);
      savePrefs(settings);
    } catch (err) {
      if (err && err.wing) {
        err.wing.problem = err.text;
        say("Fix the problem marked below, then try again.", "error");
      } else if (!(err && err.sessionEnded)) {
        say(describe(err), "error");
      }
    } finally {
      uploading = false;
      setProgress(null);
      document.querySelectorAll("#batch-settings select, #batch-settings input").forEach((el) => { el.disabled = false; });
      renderWings();
      if (signedIn) refresh();
    }
  }

  // Upload each wing's zip in turn, then ask the API to check and queue it. A wing that fails is cancelled and
  // stays in the form with the reason; the others carry on.
  async function sendAll(createdJobs, prepared) {
    const total = prepared.reduce((a, p) => a + p.blob.size, 0) || 1;
    let sent = 0;
    const failed = [];
    let queued = 0;
    for (let i = 0; i < prepared.length; i++) {
      const { w, blob } = prepared[i];
      const { job, upload: form } = createdJobs[i];
      say(`Uploading ${w.label || w.file.name} (${i + 1} of ${prepared.length})...`);
      try {
        await postToS3(form, blob, (loaded) => setProgress((sent + loaded) / total));
        sent += blob.size;
        const res = await api("POST", `/jobs/${job.id}/submit`);
        if (res.job.status === "failed") throw Object.assign(new Error(res.job.message || "The upload was rejected."), { shown: true });
        queued += 1;
        wings = wings.filter((x) => x !== w);
      } catch (err) {
        sent += blob.size;
        if (err && err.sessionEnded) throw err;
        w.problem = describe(err);
        failed.push(w);
        try { await api("POST", `/jobs/${job.id}/cancel`); } catch (_) { /* the monitor cleans up unfinished uploads */ }
      }
      renderWings();
    }
    if (failed.length) {
      say(`${plural(queued, "run is", "runs are")} queued. ${plural(failed.length, "wing", "wings")} could not be `
        + "uploaded; the reason is shown under each one.", "error");
    } else {
      say(`${plural(queued, "run is", "runs are")} queued. Follow ${queued === 1 ? "its" : "their"} progress below.`);
      $("run-files").value = "";
    }
  }

  // ---- watching runs -------------------------------------------------------------------------------------

  // Fetch the list of runs and redraw whatever changed.
  async function refresh() {
    if (!signedIn || loading) return;
    loading = true;
    try {
      const data = await api("GET", "/jobs");
      limits = { ...limits, ...(data.limits || {}) };
      worker = data.worker || null;
      jobs = data.jobs || [];
      renderAll();
    } catch (err) {
      if (!(err && err.sessionEnded)) $("runs-note").textContent = `${describe(err)} The list will try again shortly.`;
    } finally {
      loading = false;
    }
  }

  // The next refresh: soon while anything is unfinished, rarely when idle, slowly in a background tab.
  function schedule() {
    clearTimeout(timer);
    if (!signedIn) return;
    const busy = jobs.some((j) => ACTIVE.includes(j.status));
    const wait = document.hidden ? POLL_HIDDEN_MS : busy ? POLL_ACTIVE_MS : POLL_IDLE_MS;
    timer = setTimeout(async () => {
      await refresh();
      renewResultLinks();
      schedule();
    }, wait);
  }

  function renderAll() {
    $("limit-files").textContent = String(limits.max_files);
    $("set-speed").min = String(limits.speed_min);
    $("set-speed").max = String(limits.speed_max);
    renderBanner();
    // Newest upload first; the wings of one upload stay in the order they were chosen.
    const sorted = [...jobs].sort((a, b) => (b.created_at || "").localeCompare(a.created_at || "")
      || (a.seq || 0) - (b.seq || 0) || (a.label || "").localeCompare(b.label || ""));
    const list = $("runs");
    const want = sorted.map((job) => {
      const sig = JSON.stringify([job, worker && worker.online, uploading && job.status === "awaiting_upload",
        ACTIVE.includes(job.status) ? Math.floor(Date.now() / 60000) : 0]);
      let entry = cards.get(job.id);
      if (!entry || entry.sig !== sig) {
        opened.delete(job.id);                                // a rebuilt card starts with its results closed
        entry = { sig, el: renderRun(job) };
        cards.set(job.id, entry);
      }
      return entry.el;
    });
    const keep = new Set(want);
    for (const [id, entry] of cards) {
      if (!keep.has(entry.el)) { cards.delete(id); opened.delete(id); }
    }
    // Only touch cards that are new or out of place: moving a playing video out of the page would pause it.
    for (const child of [...list.children]) if (!keep.has(child)) child.remove();
    want.forEach((el, i) => { if (list.children[i] !== el) list.insertBefore(el, list.children[i] || null); });
    const active = jobs.filter((j) => ACTIVE.includes(j.status)).length;
    $("runs-note").textContent = jobs.length
      ? `${plural(jobs.length, "run", "runs")}${active ? `, ${active} unfinished` : ""}. This list updates by itself; `
        + `you can close the page and come back later (up to ${limits.max_active} unfinished runs at a time).`
      : "No runs yet. Choose STEP files above to start one.";
  }

  function renderBanner() {
    const el = $("worker-banner");
    if (!worker) { el.hidden = true; return; }
    el.hidden = false;
    if (worker.online) {
      el.className = "banner ok";
      el.textContent = "The worker is online. Queued runs start as soon as the ones before them finish.";
    } else {
      const seen = worker.last_seen_seconds === null || worker.last_seen_seconds === undefined ? ""
        : ` (last seen ${fmtDuration(worker.last_seen_seconds)} ago)`;
      el.className = "banner warn";
      el.textContent = `The worker program on the owner's PC is offline${seen}. New runs wait in the queue and start `
        + "when it is running again.";
    }
  }

  // One line describing a run's settings.
  function optionsLine(o) {
    const bits = [SOLVER_NAMES[o.solver] || o.solver, PRESET_NAMES[o.preset] || o.preset,
      `${o.speed} m/s`, GROUND_NAMES[o.ground] || o.ground, `drawn in ${o.units}`];
    if (o.half_model) bits.push("half-domain mesh");
    return bits.filter(Boolean).join(" · ");
  }

  // The seven-step checklist.
  function stepList(job) {
    const stages = job.stages || {};
    return h("ol", { class: "steps" }, STEPS.map(([key, label]) => {
      const st = stages[key] || {};
      const state = ["done", "running", "failed"].includes(st.state) ? st.state : "pending";
      const mark = { done: "✓", running: "●", failed: "✗" }[state] || "○";
      const word = { done: "done", running: "in progress", failed: "failed", pending: "not started" }[state];
      return h("li", { "data-state": state },
        h("span", { class: "mark", "aria-hidden": "true", text: mark }),
        h("span", { text: label }),
        h("span", { class: "sr", text: ` (${word})` }),
        state === "done" && typeof st.seconds === "number" ? h("span", { class: "time", text: fmtDuration(st.seconds) }) : null);
    }));
  }

  // The "what is it doing now" line for a run in progress.
  function nowLine(job) {
    const p = job.progress || {};
    const bits = [job.stage || "Starting"];
    if (typeof p.iterations === "number" && p.iterations > 0) bits.push(`${plural(p.iterations, "iteration", "iterations")} so far`);
    if (typeof p.cells === "number" && p.cells > 0) bits.push(fmtCells(p.cells));
    return bits.join(" · ");
  }

  // Two-step cancel button: the first click asks for confirmation on the button itself.
  function cancelButton(job) {
    let armed = null;
    const button = h("button", { type: "button", class: "secondary small", text: "Cancel this run" });
    button.addEventListener("click", async () => {
      if (!armed) {
        button.textContent = "Click again to cancel";
        armed = setTimeout(() => { armed = null; button.textContent = "Cancel this run"; }, 5000);
        return;
      }
      clearTimeout(armed);
      button.disabled = true;
      button.textContent = "Cancelling...";
      try { await api("POST", `/jobs/${job.id}/cancel`); } catch (err) { $("runs-note").textContent = describe(err); }
      refresh();
    });
    return button;
  }

  // The card for one run.
  function renderRun(job) {
    const s = job.status;
    const queuedText = typeof job.runs_ahead === "number"
      ? (job.runs_ahead === 0 ? "Next in line." : `${plural(job.runs_ahead, "run", "runs")} ahead of this one.`) : "Waiting in the queue.";
    const meta = [fmtTime(job.created_at), job.filename].filter(Boolean).join(" · ");
    const card = h("li", { class: "run", "data-status": s },
      h("div", { class: "run-top" },
        h("h3", { class: "run-title", text: job.label || job.filename || "Run" }),
        h("span", { class: `badge ${s}`, text: STATUS_WORDS[s] || s })),
      h("p", { class: "meta", text: meta }),
      h("p", { class: "meta", text: optionsLine(job.options || {}) }));

    if (s === "awaiting_upload") {
      card.append(h("p", { class: "now", text: uploading ? "Uploading from this browser..." : "Waiting for the upload to finish." }));
      if (!uploading) {
        card.append(h("p", { class: "hint", text: "If the page was closed during the upload, this run is cancelled "
          + "automatically after a couple of hours." }));
      }
    } else if (s === "queued") {
      card.append(h("p", { class: "now", text: queuedText }));
      if (worker && !worker.online) {
        card.append(h("p", { class: "hint", text: "The worker is offline, so it will start when the worker is back." }));
      }
    } else if (s === "running") {
      const since = secondsSince(job.started_at);
      const last = secondsSince(job.updated_at);
      card.append(h("p", { class: "now", text: nowLine(job) }), stepList(job),
        h("p", { class: "hint", text: [since !== null ? `Running for ${fmtDuration(since)}` : "",
          last !== null ? `last update ${fmtDuration(last)} ago` : ""].filter(Boolean).join(", ") + "." }));
      if (job.cancel_requested) card.append(h("p", { class: "hint", text: "Cancel requested. The worker will stop it shortly." }));
    }
    if (job.message && s !== "running" && s !== "queued") {
      card.append(h("p", { class: `note ${s === "failed" || s === "stopped" ? "problem-note" : ""}`, text: job.message }));
    } else if (job.message) {
      card.append(h("p", { class: "note", text: job.message }));
    }
    if (s === "done") card.append(...resultsSection(job));
    else if (s === "failed" || s === "stopped") card.append(h("details", {}, h("summary", { text: "Steps reached" }), stepList(job)));
    if (ACTIVE.includes(s) && !job.cancel_requested) card.append(h("div", { class: "actions" }, cancelButton(job)));
    return card;
  }

  // ---- results -------------------------------------------------------------------------------------------

  // A plain-words caution to sit above the numbers of a rough run: a Preview run, or any run whose mesh the
  // RESOLUTION check found too coarse for the part. Returns "" when the numbers need no such caution.
  function roughNote(job, gates) {
    const coarse = gates.some((g) => g.code === "RESOLUTION" && ["WARN", "FAIL"].includes(g.level));
    const preview = (job.options || {}).preset === "quick";
    if (coarse && preview) {
      return "Preview only: the mesh is too coarse for this part to trust these numbers. "
        + "Run it again at Standard quality for numbers you can quote.";
    }
    if (coarse) {
      return "The mesh is too coarse for this part, so treat these numbers as a rough first look. "
        + "Check the model's units, or use a larger model.";
    }
    if (preview) {
      return "Preview quality: good for checking that the model and the flow look right, not for numbers you "
        + "want to quote. Run it again at Standard quality for those.";
    }
    return "";
  }

  // The numbers, quality checks and (once opened) pictures, video and downloads of a finished run.
  function resultsSection(job) {
    const sum = job.summary || {};
    const gates = Array.isArray(sum.gates) ? sum.gates : [];
    const out = [];
    const took = secondsBetween(job.started_at, job.finished_at);
    if (took !== null) out.push(h("p", { class: "meta", text: `Took ${fmtDuration(took)}.` }));
    const note = roughNote(job, gates);
    if (note) out.push(h("p", { class: "banner warn rough-note", role: "note", text: note }));
    const tiles = [["Drag, Cd", num(sum.cd, 4)], ["Lift, Cl", num(sum.cl, 4)], ["CdA (m²)", num(sum.s_cd, 5)],
      ["ClA (m²)", num(sum.s_cl, 5)], ["Reference area (m²)", num(sum.aref_m2, 4)],
      ["Iterations", typeof sum.iterations === "number" ? String(sum.iterations) : "-"],
      ["Mesh", typeof sum.cells === "number" ? fmtCells(sum.cells).replace(" cells", "") : "-"],
      ["Checks", sum.result || "-"]];
    out.push(h("dl", { class: "numbers" }, tiles.filter(([, v]) => v !== "-").map(([k, v]) => h("div", {}, h("dt", { text: k }),
      h("dd", { text: v })))));
    out.push(h("p", { class: "hint", text: "Negative lift means downforce. CdA and ClA are the coefficient times "
      + "the reference area, so different wings can be compared directly." }));
    if (gates.length) {
      const serious = gates.some((g) => ["FAIL", "WARN", "ACTION"].includes(g.level));
      out.push(h("details", { open: serious }, h("summary", { text: `Quality checks (${gates.length})` }),
        h("ul", { class: "gates" }, gates.map((g) => h("li", { class: g.level }, h("strong", { text: `${g.level} ` }),
          g.message)))));
    }
    out.push(h("details", {}, h("summary", { text: "Steps and timings" }), stepList(job)));
    if ((job.files || []).length) {
      const body = h("div", {});
      const panel = h("details", { class: "results" }, h("summary", { text: "Pictures, video and downloads" }), body);
      panel.addEventListener("toggle", () => {
        if (!panel.open) { opened.delete(job.id); return; }
        if (!opened.has(job.id)) loadResults(job.id, body);
      });
      out.push(panel);
    }
    return out;
  }

  // Fetch fresh download links for one run and draw its pictures, video and files.
  async function loadResults(id, body) {
    const state = { at: Date.now(), urls: [] };
    opened.set(id, state);
    body.replaceChildren(h("p", { class: "hint", text: "Loading..." }));
    try {
      const { job } = await api("GET", `/jobs/${id}`);
      state.at = Date.now();
      body.replaceChildren(...buildResults(job.files || [], state));
    } catch (err) {
      opened.delete(id);
      if (!(err && err.sessionEnded)) body.replaceChildren(h("p", { class: "problem", text: describe(err) }));
    }
  }

  // Pictures, the video and the download links. Each element is recorded so its link can be renewed later.
  function buildResults(files, state) {
    const track = (name, el, attr) => { state.urls.push({ name, el, attr }); return el; };
    const images = files.filter((f) => f.kind === "image");
    const videos = files.filter((f) => f.kind === "video");
    const others = files.filter((f) => f.kind !== "image" && f.kind !== "video");
    const out = [];
    if (images.length) {
      out.push(h("div", { class: "gallery" }, images.map((f) => h("figure", {},
        track(f.name, h("a", { href: f.url, target: "_blank", rel: "noopener" },
          track(f.name, h("img", { src: f.url, alt: f.title || f.name, loading: "lazy" }), "src")), "href"),
        h("figcaption", { text: f.title || f.name })))));
    }
    for (const f of videos) {
      out.push(track(f.name, h("video", { controls: true, preload: "metadata", playsinline: true, src: f.url,
        "aria-label": f.title || f.name }), "src"));
    }
    out.push(h("ul", { class: "downloads" }, [...videos, ...others].map((f) => h("li", {},
      track(f.name, h("a", { href: f.url, download: f.name, target: "_blank", rel: "noopener",
        text: `${f.kind === "video" ? "Download video" : f.title || f.name} (${fmtBytes(f.size || 0)})` }), "href")))));
    out.push(h("p", { class: "hint", text: "These links work for 15 minutes; close and reopen this section for new ones." }));
    return out;
  }

  // Result links expire after 15 minutes. For open result panels older than 10, fetch new links and swap them in
  // place (a video that is playing keeps its current link so it is not interrupted).
  async function renewResultLinks() {
    for (const [id, state] of opened) {
      if (Date.now() - state.at < LINK_REFRESH_MS) continue;
      try {
        const { job } = await api("GET", `/jobs/${id}`);
        const fresh = new Map((job.files || []).map((f) => [f.name, f.url]));
        for (const { name, el, attr } of state.urls) {
          if (el.tagName === "VIDEO" && !el.paused) continue;
          if (fresh.has(name)) el.setAttribute(attr, fresh.get(name));
        }
        state.at = Date.now();
      } catch (_) { /* try again at the next refresh */ }
    }
  }

  // ---- wiring --------------------------------------------------------------------------------------------

  function applyPrefs() {
    const prefs = loadPrefs();
    const pick = (id, value) => {
      const el = $(id);
      if (value === undefined || value === null) return;
      const before = el.value;
      el.value = String(value);
      if (el.tagName === "SELECT" && el.value !== String(value)) el.value = before;
    };
    pick("set-solver", prefs.solver);
    pick("set-preset", prefs.preset);
    pick("set-ground", prefs.ground);
    if (Number.isFinite(prefs.speed)) $("set-speed").value = String(prefs.speed);
  }

  function start() {
    signedIn = true;
    applyPrefs();
    renderWings();
    say("");
    refresh().then(schedule);
  }

  // Forget everything shown, so the next person to use this browser sees none of it.
  function stop() {
    signedIn = false;
    clearTimeout(timer);
    if (upload) upload.abort();
    wings = [];
    jobs = [];
    worker = null;
    uploading = false;
    cards.clear();
    opened.clear();
    $("runs").replaceChildren();
    $("wing-list").replaceChildren();
    $("run-files").value = "";
    $("worker-banner").hidden = true;
    $("runs-note").textContent = "";
    say("");
    setProgress(null);
  }

  $("run-files").addEventListener("change", (e) => { addFiles([...e.target.files]); e.target.value = ""; });
  $("form-runs").addEventListener("submit", (e) => { e.preventDefault(); startRuns(); });
  $("refresh").addEventListener("click", async () => { await refresh(); schedule(); });
  $("form-runs").addEventListener("dragover", (e) => {
    if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) e.preventDefault();
  });
  $("form-runs").addEventListener("drop", (e) => {
    e.preventDefault();
    if (!uploading) addFiles([...e.dataTransfer.files]);
  });
  document.addEventListener("visibilitychange", () => {
    if (signedIn && !document.hidden) { refresh().then(schedule); }
  });
  window.addEventListener("beforeunload", (e) => {
    if (uploading) { e.preventDefault(); e.returnValue = ""; }
  });
  document.addEventListener("cfd:signedin", start);
  document.addEventListener("cfd:signedout", stop);
})();
