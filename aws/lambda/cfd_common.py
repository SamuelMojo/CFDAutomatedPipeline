"""Rules shared by the site's API (Lambda), the admin tool and the home-PC worker.

Keeping them in one file means the three programs cannot disagree about what a job looks like, which
options are allowed, or how a STEP zip is checked.

A job is one DynamoDB item. Its status moves along one path:

    awaiting_upload -> queued -> running -> done | stopped | failed
    (cancelled can replace any state before the end)

Nothing here needs AWS libraries, so the tests run anywhere.
"""
from __future__ import annotations

import io
import math
import re
import zipfile
from datetime import datetime, timezone
from decimal import Decimal

# --------------------------------------------------------------------------- #
# Job states and stages
# --------------------------------------------------------------------------- #
AWAITING_UPLOAD, QUEUED, RUNNING = "awaiting_upload", "queued", "running"
DONE, STOPPED, FAILED, CANCELLED = "done", "stopped", "failed", "cancelled"
ACTIVE = (AWAITING_UPLOAD, QUEUED, RUNNING)        # a job in one of these still counts against the user's limit
FINISHED = (DONE, STOPPED, FAILED, CANCELLED)

# The stages shown as a checklist, in order. The first six are run_job.py's own stage names.
STAGES = ("inspect", "case", "mesh", "mesh gates", "solve", "finish", "render")

# What the site says while a stage is under way (the run_job.py stage names are the keys).
STAGE_TEXT = {"inspect": "Checking the geometry", "case": "Setting up the case", "mesh": "Meshing",
              "mesh gates": "Checking the mesh", "solve": "Solving", "finish": "Finishing the case",
              "render": "Making pictures and video"}

WORKER_KEY = {"job_id": "_worker"}                 # the worker's heartbeat item lives in the jobs table
WORKER_ONLINE_SECONDS = 90                         # the worker counts as online if it checked in this recently

# --------------------------------------------------------------------------- #
# Options a user may choose
# --------------------------------------------------------------------------- #
SOLVERS = {"SA": "SpalartAllmaras", "SST": "kOmegaSST"}     # label on the site -> OpenFOAM model name
PRESETS = ("quick", "standard")
UNITS = ("mm", "cm", "m", "in")
GROUNDS = ("moving", "fixed")
SPEED_RANGE = (5.0, 100.0)                         # free-stream speed, m/s
DEFAULT_SPEED = 20.0
MAX_FILES_PER_BATCH = 5
STEP_MAGIC = b"ISO-10303-21"                       # every STEP file starts with this


class ValidationError(Exception):
    """The request is not acceptable; the message is safe to show to the user."""


def clean_text(value, max_len: int) -> str:
    """Return value as plain one-line text: control characters removed, spaces collapsed, cut to max_len."""
    text = "" if value is None else str(value)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:max_len]


def validate_settings(raw) -> dict:
    """Check the batch-wide settings (solver, preset, speed, ground) and return them in a clean form."""
    if not isinstance(raw, dict):
        raise ValidationError("Settings are missing.")
    solver = raw.get("solver", "SA")
    if solver not in SOLVERS:
        raise ValidationError(f"Solver must be one of {', '.join(SOLVERS)}.")
    preset = raw.get("preset", "quick")
    if preset not in PRESETS:
        raise ValidationError(f"Quality must be one of {', '.join(PRESETS)}.")
    ground = raw.get("ground", "moving")
    if ground not in GROUNDS:
        raise ValidationError(f"Ground must be one of {', '.join(GROUNDS)}.")
    try:
        speed = float(raw.get("speed", DEFAULT_SPEED))
    except (TypeError, ValueError):
        raise ValidationError("Speed must be a number.") from None
    if not math.isfinite(speed) or not SPEED_RANGE[0] <= speed <= SPEED_RANGE[1]:
        raise ValidationError(f"Speed must be between {SPEED_RANGE[0]:g} and {SPEED_RANGE[1]:g} m/s.")
    return {"solver": solver, "preset": preset, "speed": round(speed, 3), "ground": ground}


def validate_file(raw, max_bytes: int) -> dict:
    """Check one wing's entry (file name, zip size, label, units, half model) and return it in a clean form."""
    if not isinstance(raw, dict):
        raise ValidationError("A file entry is malformed.")
    filename = clean_text(raw.get("filename"), 120)
    if not filename:
        raise ValidationError("Every file needs a name.")
    size = raw.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise ValidationError(f"{filename}: the file is empty or its size is missing.")
    if size > max_bytes:
        raise ValidationError(f"{filename}: the upload is {size / 1e6:.0f} MB; the limit is {max_bytes / 1e6:.0f} MB.")
    units = raw.get("units")
    if units not in UNITS:
        raise ValidationError(f"{filename}: choose the units the model is drawn in ({', '.join(UNITS)}).")
    stem = re.sub(r"\.(zip|step|stp)$", "", filename, flags=re.I)
    return {"filename": filename, "size": size, "units": units, "half_model": bool(raw.get("half_model", False)),
            "label": clean_text(raw.get("label"), 60) or clean_text(stem, 60) or "wing"}


def case_options(settings: dict, file: dict) -> dict:
    """The options that go into the pipeline's job file, built from validated values only."""
    return {"units": file["units"], "half_model": file["half_model"], "solver": settings["solver"],
            "preset": settings["preset"], "speed": settings["speed"], "ground": settings["ground"]}


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #
def utc_now() -> str:
    """The current UTC time as an ISO string such as 2026-11-12T15:20:11Z (sorts correctly as text)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def seconds_since(stamp: str | None, now: str | None = None) -> float | None:
    """Seconds between an ISO stamp and now (or `now`); None if the stamp is missing or unreadable."""
    try:
        then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
        ref = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ") if now else datetime.now(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None
    return (ref - then).total_seconds()


# --------------------------------------------------------------------------- #
# DynamoDB helpers (work on boto3 Table objects; the tests use an in-memory stand-in)
# --------------------------------------------------------------------------- #
def to_ddb(obj):
    """Convert Python values to what DynamoDB accepts: floats become Decimals; NaN and infinity are refused."""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError("DynamoDB cannot store NaN or infinity")
        return Decimal(repr(obj))
    if isinstance(obj, dict):
        return {k: to_ddb(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_ddb(v) for v in obj]
    return obj


def from_ddb(obj):
    """Convert DynamoDB values back to plain Python: whole Decimals become ints, the rest floats."""
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: from_ddb(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [from_ddb(v) for v in obj]
    return obj


def is_conditional_failure(exc: Exception) -> bool:
    """True if exc is DynamoDB saying a condition was not met (someone else changed the item first)."""
    code = getattr(exc, "response", {}).get("Error", {}).get("Code")
    return code == "ConditionalCheckFailedException"


def update_fields(table, key: dict, fields: dict, only_if: tuple | None = None) -> dict:
    """SET the given fields on one item and return the item as it is afterwards.

    only_if=("status", ("queued",)) makes the write happen only while that attribute holds one of the listed
    values; otherwise DynamoDB's conditional failure is raised (see is_conditional_failure).
    """
    names, values, sets = {}, {}, []
    for i, (name, value) in enumerate(fields.items()):
        names[f"#f{i}"], values[f":v{i}"] = name, to_ddb(value)
        sets.append(f"#f{i} = :v{i}")
    args = {"Key": key, "UpdateExpression": "SET " + ", ".join(sets), "ReturnValues": "ALL_NEW"}
    if only_if:
        attribute, allowed = only_if
        names["#c"] = attribute
        marks = []
        for j, value in enumerate(allowed):
            values[f":c{j}"] = value
            marks.append(f":c{j}")
        args["ConditionExpression"] = f"#c IN ({', '.join(marks)})"
    args["ExpressionAttributeNames"], args["ExpressionAttributeValues"] = names, values
    return from_ddb(table.update_item(**args)["Attributes"])


def queue_order(jobs: list) -> list:
    """Runs in the order the worker takes them: oldest first, and the wings of one upload in the order the person
    chose them (the index only sorts by creation second, so wings made together would otherwise tie).
    """
    return sorted(jobs, key=lambda j: (j.get("created_at", ""), j.get("seq", 0), j.get("job_id", "")))


def query_index(table, index: str, attribute: str, value: str, newest_first: bool = False, limit: int = 100) -> list:
    """Items whose `attribute` (the index's key) equals `value`, ordered by creation time."""
    out = table.query(IndexName=index, KeyConditionExpression="#k = :k",
                      ExpressionAttributeNames={"#k": attribute}, ExpressionAttributeValues={":k": value},
                      ScanIndexForward=not newest_first, Limit=limit)
    return [from_ddb(item) for item in out.get("Items", [])]


# --------------------------------------------------------------------------- #
# Telling the owner when something goes wrong
# --------------------------------------------------------------------------- #
ALERT_PREFIX = "[CFD site] "


def send_alert(sns, topic_arn: str, subject: str, text: str) -> bool:
    """Email the owner through the SNS topic. Never raises: an alert must not break the thing that is failing.

    Returns True if SNS accepted the message. The subject is cut to the ASCII text SNS allows (100 characters).
    """
    if not topic_arn:
        return False
    subject = (ALERT_PREFIX + clean_text(subject, 200)).encode("ascii", "ignore").decode()[:100]
    try:
        sns.publish(TopicArn=topic_arn, Subject=subject, Message=text[:20000])
        return True
    except Exception as exc:                                        # noqa: BLE001 - see docstring
        print(f"alert not sent: {type(exc).__name__}: {exc}")
        return False


def alert_once(table, sns, topic_arn: str, key: str, subject: str, text: str, min_seconds: int = 600) -> bool:
    """send_alert, but at most once per min_seconds for the same key, so a repeating fault cannot flood the inbox.

    The time of the last alert is kept as a small item in the jobs table. Two alerts at the very same moment may
    both go out; that is acceptable.
    """
    state_key = {"job_id": f"_alert_{key}"}
    try:
        last = (table.get_item(Key=state_key).get("Item") or {}).get("last_at")
        if last and (seconds_since(last) or 1e9) < min_seconds:
            return False
        table.put_item(Item={**state_key, "last_at": utc_now()})
    except Exception as exc:                                        # noqa: BLE001 - alerting must never raise
        print(f"alert state not saved: {type(exc).__name__}: {exc}")
    return send_alert(sns, topic_arn, subject, text)


# --------------------------------------------------------------------------- #
# Checking an uploaded zip without downloading it
# --------------------------------------------------------------------------- #
class RangeFile(io.RawIOBase):
    """A read-only, seekable file backed by ranged downloads, so zipfile can read a big zip's directory cheaply.

    fetch(first, last) must return bytes first..last inclusive. One 256 KB block is cached, and the number of
    downloads is capped so a hostile file cannot make the reader loop for ever.
    """

    BLOCK = 256 * 1024

    def __init__(self, fetch, size: int, max_fetches: int = 40):
        """fetch: the ranged-download function; size: the object's size in bytes."""
        super().__init__()
        self._fetch, self._size, self._pos = fetch, size, 0
        self._start, self._block = 0, b""
        self._left = max_fetches

    def readable(self) -> bool:
        """Always True: this file can be read."""
        return True

    def seekable(self) -> bool:
        """Always True: zipfile jumps to the end and back."""
        return True

    def tell(self) -> int:
        """The current read position."""
        return self._pos

    def seek(self, offset: int, whence: int = 0) -> int:
        """Move the read position (0 from the start, 1 from here, 2 from the end)."""
        base = {0: 0, 1: self._pos, 2: self._size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def read(self, n: int = -1) -> bytes:
        """Read up to n bytes (everything to the end if n is negative), downloading blocks as needed."""
        end = self._size if n is None or n < 0 else min(self._size, self._pos + n)
        out = []
        while self._pos < end:
            if not self._start <= self._pos < self._start + len(self._block):
                if self._left <= 0:
                    raise ValueError("The zip needs too many reads to inspect.")
                self._left -= 1
                self._start = self._pos - self._pos % self.BLOCK
                self._block = self._fetch(self._start, min(self._start + self.BLOCK, self._size) - 1)
                if not self._block:
                    break
            part = self._block[self._pos - self._start:end - self._start]
            out.append(part)
            self._pos += len(part)
        return b"".join(out)


def is_step_header(first_bytes: bytes) -> bool:
    """True if the bytes start like a STEP file (ISO-10303-21), ignoring a byte-order mark and leading spaces."""
    return first_bytes.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(STEP_MAGIC)


def inspect_step_zip(fetch, size: int, max_step_bytes: int, max_ratio: int = 200) -> dict:
    """Check that a stored object is a normal zip holding exactly one STEP file, and return facts about it.

    Returns {"entry": name, "uncompressed": bytes, "compressed": bytes}. Raises ValidationError, with a message
    for the user, if it is not a zip, is encrypted, holds zero or several files, holds something that is not a
    STEP file, is larger than max_step_bytes when unpacked, or squeezes by an unbelievable ratio (a zip bomb).
    Only the zip's directory and the first bytes of the STEP file are read.
    """
    try:
        with zipfile.ZipFile(RangeFile(fetch, size)) as zf:
            files = [i for i in zf.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")
                     and not i.filename.rsplit("/", 1)[-1].startswith(("._", ".DS_Store"))]
            if not files:
                raise ValidationError("The zip is empty.")
            if len(files) > 1:
                raise ValidationError(f"The zip holds {len(files)} files. Put one STEP file in each zip; "
                                      "to run several wings, add several files in one upload.")
            info = files[0]
            if not info.filename.lower().endswith((".step", ".stp")):
                raise ValidationError("The file inside the zip is not a .step or .stp file.")
            if info.flag_bits & 0x1:
                raise ValidationError("The zip is password-protected; upload it without a password.")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ValidationError("The zip uses an unsupported compression method; use normal zip (deflate).")
            if info.file_size > max_step_bytes:
                raise ValidationError(f"The STEP file is {info.file_size / 1e6:.0f} MB unpacked; "
                                      f"the limit is {max_step_bytes / 1e6:.0f} MB.")
            if info.compress_size and info.file_size / info.compress_size > max_ratio:
                raise ValidationError("The zip's compression ratio is not believable; it was refused.")
            with zf.open(info) as f:
                head = f.read(256)
            if not is_step_header(head):
                raise ValidationError("The file inside the zip does not look like a STEP file "
                                      "(no ISO-10303-21 header).")
            return {"entry": info.filename, "uncompressed": info.file_size, "compressed": info.compress_size}
    except zipfile.BadZipFile:
        raise ValidationError("That is not a valid zip file.") from None
    except (ValueError, OSError, NotImplementedError, RuntimeError, EOFError, zipfile.LargeZipFile) as exc:
        raise ValidationError(f"The zip could not be read ({type(exc).__name__}).") from None
