"""Shared test setup: makes the Lambda code importable and provides in-memory stand-ins for AWS."""
import copy
import io
import re
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lambda"))
sys.path.insert(0, str(ROOT / "worker"))

import api  # noqa: E402
import cfd_common as cc  # noqa: E402

STEP_TEXT = b"ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('test'),'2;1');\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"


class FakeClientError(Exception):
    """Looks like botocore's ClientError: carries .response['Error']['Code']."""

    def __init__(self, code):
        """code: the AWS error code, for example ConditionalCheckFailedException."""
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def _no_floats(obj):
    """Real DynamoDB refuses Python floats; the stand-in does too, so a missing to_ddb() shows up in tests."""
    if isinstance(obj, float):
        raise TypeError("Float types are not supported. Use Decimal types instead.")
    if isinstance(obj, dict):
        for v in obj.values():
            _no_floats(v)
    if isinstance(obj, list):
        for v in obj:
            _no_floats(v)


class FakeTable:
    """An in-memory DynamoDB table that understands only the call shapes this project uses."""

    INDEXES = {"by_user": ("user_sub", "created_at"), "by_status": ("status", "created_at")}

    def __init__(self, key="job_id", indexes=None):
        """key: the hash key's name; indexes: name -> (hash attribute, range attribute)."""
        self.key, self.items = key, {}
        self.indexes = self.INDEXES if indexes is None else indexes

    def put_item(self, Item, ConditionExpression=None):
        """Store an item; supports only the attribute_not_exists(<key>) condition."""
        _no_floats(Item)
        if ConditionExpression is not None:
            assert ConditionExpression == f"attribute_not_exists({self.key})", ConditionExpression
            if Item[self.key] in self.items:
                raise FakeClientError("ConditionalCheckFailedException")
        self.items[Item[self.key]] = copy.deepcopy(Item)

    def get_item(self, Key):
        """Return {'Item': ...} or {} like DynamoDB."""
        item = self.items.get(Key[self.key])
        return {"Item": copy.deepcopy(item)} if item is not None else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues,
                    ReturnValues=None, ConditionExpression=None):
        """SET #f0 = :v0, ... with an optional '#c IN (:c0, ...)' condition; creates the item if it is missing."""
        _no_floats(ExpressionAttributeValues)
        item = self.items.get(Key[self.key])
        if ConditionExpression:
            m = re.fullmatch(r"#c IN \(([^)]*)\)", ConditionExpression)
            assert m, ConditionExpression
            allowed = [ExpressionAttributeValues[v.strip()] for v in m.group(1).split(",")]
            if item is None or item.get(ExpressionAttributeNames["#c"]) not in allowed:
                raise FakeClientError("ConditionalCheckFailedException")
        item = item if item is not None else dict(Key)
        assert UpdateExpression.startswith("SET ")
        for part in UpdateExpression[4:].split(", "):
            m = re.fullmatch(r"(#f\d+) = (:v\d+)", part)
            assert m, part
            item[ExpressionAttributeNames[m.group(1)]] = ExpressionAttributeValues[m.group(2)]
        self.items[Key[self.key]] = item
        return {"Attributes": copy.deepcopy(item)}

    def query(self, IndexName, KeyConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues,
              ScanIndexForward=True, Limit=None):
        """Items of one index partition, ordered by the index's range attribute."""
        assert KeyConditionExpression == "#k = :k"
        hash_attr, range_attr = self.indexes[IndexName]
        assert ExpressionAttributeNames["#k"] == hash_attr
        rows = [i for i in self.items.values() if i.get(hash_attr) == ExpressionAttributeValues[":k"]
                and range_attr in i]
        rows.sort(key=lambda i: (i[range_attr], i[self.key]), reverse=not ScanIndexForward)
        return {"Items": copy.deepcopy(rows[:Limit] if Limit else rows)}


class FakeS3:
    """An in-memory S3 with the five calls the API makes."""

    def __init__(self):
        """Start empty."""
        self.objects, self.posts = {}, []

    def generate_presigned_post(self, Bucket, Key, Conditions, ExpiresIn):
        """Return a form like boto3's, and remember the request."""
        self.posts.append({"Bucket": Bucket, "Key": Key, "Conditions": Conditions})
        return {"url": f"https://{Bucket}.s3.eu-west-2.amazonaws.com/", "fields": {"key": Key, "policy": "p"}}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        """Return a recognisable fake link."""
        extra = "&dl=1" if "ResponseContentDisposition" in Params else ""
        return f"https://{Params['Bucket']}.s3.eu-west-2.amazonaws.com/{Params['Key']}?sig=x{extra}"

    def head_object(self, Bucket, Key):
        """Size of an object, or a 404 error."""
        if (Bucket, Key) not in self.objects:
            raise FakeClientError("404")
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def get_object(self, Bucket, Key, Range):
        """A ranged read, 'bytes=first-last' inclusive."""
        first, last = map(int, Range.removeprefix("bytes=").split("-"))
        self.fetches = getattr(self, "fetches", 0) + 1
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)][first:last + 1])}


class FakeSNS:
    """Remembers every message published."""

    def __init__(self, fail=False):
        """fail=True makes publish raise, to test that alerts never break the caller."""
        self.sent, self.fail = [], fail

    def publish(self, TopicArn, Subject, Message):
        """Record the message (or raise)."""
        if self.fail:
            raise RuntimeError("sns down")
        self.sent.append({"TopicArn": TopicArn, "Subject": Subject, "Message": Message})


def make_zip(entries: dict, compress=True) -> bytes:
    """A zip file in memory from {name: bytes}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def env(monkeypatch):
    """Lambda settings as Terraform would set them."""
    for k, v in {"JOBS_TABLE": "jobs", "REQUESTS_TABLE": "requests", "UPLOADS_BUCKET": "up", "RESULTS_BUCKET": "res",
                 "TOPIC_ARN": "arn:topic", "MAX_UPLOAD_MB": "1", "MAX_STEP_MB": "2", "MAX_ACTIVE_JOBS": "6",
                 "MAX_JOBS_PER_DAY": "8"}.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def aws(env, monkeypatch):
    """Fresh in-memory AWS wired into the API module."""
    be = api.Backends(FakeTable(), FakeTable(key="email", indexes={}), FakeS3(), FakeSNS())
    monkeypatch.setattr(api, "_backends", be)
    return be


def event(route, body=None, sub="user-1", email="a@example.com", params=None, signed_in=True):
    """An API Gateway (payload 2.0) event for the given route."""
    import json
    ev = {"routeKey": route, "pathParameters": params or {}, "body": json.dumps(body) if body is not None else None,
          "requestContext": {"http": {"sourceIp": "203.0.113.9"}}}
    if signed_in:
        ev["requestContext"]["authorizer"] = {"jwt": {"claims": {"sub": sub, "email": email}}}
    return ev


def call(route, body=None, **kw):
    """Run the handler and return (status, parsed body)."""
    import json
    out = api.handler(event(route, body, **kw))
    return out["statusCode"], json.loads(out["body"])


def new_run(aws, sub="user-1", label="wing", units="mm", **settings):
    """Create one run through the API and upload a valid zip for it; returns (job id, upload key)."""
    st, out = call("POST /jobs", {"settings": settings or {"solver": "SA"},
                                  "files": [{"filename": f"{label}.zip", "size": 500, "units": units}]}, sub=sub)
    assert st == 200, out
    job_id = out["jobs"][0]["job"]["id"]
    key = out["jobs"][0]["upload"]["fields"]["key"]
    aws.s3.objects[("up", key)] = make_zip({f"{label}.step": STEP_TEXT})
    return job_id, key


__all__ = ["cc"]
