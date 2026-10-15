import asyncio
import logging
import re

import httpx
import pytest

from pta2ccs.printjob import build_job, send_job


def make_job(**overrides) -> dict:
    kwargs = {"source": "print(1)\n", "filename": "3-1.py", "lang": "python3",
              "tname": "鸡你太美队", "team": "D22", "location": "A12",
              "group": "正式队伍"}
    kwargs.update(overrides)
    return build_job(**kwargs)


# ---------------------------------------------------------------------------
# build_job — pure multipart kwargs
# ---------------------------------------------------------------------------

def test_build_job_files_tuple_and_data():
    job = make_job()
    name, body, mime = job["files"]["file"]
    assert name == "3-1.py"
    assert body == "print(1)\n".encode("utf-8")
    assert mime == "text/plain"
    assert job["data"] == {"filename": "3-1.py", "lang": "python3",
                           "tname": "鸡你太美队", "team": "D22",
                           "location": "A12", "group": "正式队伍"}


def test_build_job_omits_empty_location_and_group():
    job = make_job(location=None, group="")
    assert "location" not in job["data"]
    assert "group" not in job["data"]
    assert job["data"]["team"] == "D22"  # other fields unaffected


def test_build_job_keeps_location_without_group():
    job = make_job(group=None)
    assert job["data"]["location"] == "A12"
    assert "group" not in job["data"]


def test_build_job_source_utf8_encoded():
    job = make_job(source="打印内容 = '你好'\n")
    _name, body, _mime = job["files"]["file"]
    assert body == "打印内容 = '你好'\n".encode("utf-8")
    assert "你好" in body.decode("utf-8")


def test_build_job_empty_source_encodes_empty_bytes():
    job = make_job(source="")
    assert job["files"]["file"][1] == b""


# ---------------------------------------------------------------------------
# send_job — httpx.MockTransport, never the network
# ---------------------------------------------------------------------------

def parse_multipart(body: bytes, content_type: str) -> dict:
    boundary = content_type.partition("boundary=")[2].strip().encode()
    delim = b"--" + boundary
    assert body.startswith(delim), "unexpected multipart preamble"
    parts: dict = {}
    for chunk in body[len(delim):].split(b"\r\n" + delim):
        if chunk.startswith(b"--"):  # closing delimiter (with/without CRLF)
            continue
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        headers, _, payload = chunk.partition(b"\r\n\r\n")
        m = re.search(rb'name="([^"]+)"', headers)
        if not m:
            continue
        fname = re.search(rb'filename="([^"]*)"', headers)
        parts[m.group(1).decode()] = (
            fname.group(1).decode() if fname else None, payload)
    return parts


def client_capturing(capture: dict, status_code: int = 200,
                     exc: Exception | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        capture["request"] = request
        if exc is not None:
            raise exc
        return httpx.Response(status_code, json={"ok": status_code < 300})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_send_job_posts_multipart_fields():
    capture: dict = {}
    client = client_capturing(capture)
    ok = asyncio.run(send_job(client, "http://printer.local/print",
                              make_job()))
    assert ok is True
    req = capture["request"]
    assert str(req.url) == "http://printer.local/print"
    assert req.headers["content-type"].startswith("multipart/form-data")
    parts = parse_multipart(req.content, req.headers["content-type"])
    assert parts["file"][0] == "3-1.py"
    assert parts["file"][1] == "print(1)\n".encode("utf-8")
    assert parts["filename"][1] == b"3-1.py"
    assert parts["lang"][1] == b"python3"
    assert parts["tname"][1] == "鸡你太美队".encode("utf-8")
    assert parts["team"][1] == b"D22"
    assert parts["location"][1] == b"A12"
    assert parts["group"][1] == "正式队伍".encode("utf-8")


def test_send_job_omits_optional_fields():
    capture: dict = {}
    client = client_capturing(capture)
    ok = asyncio.run(send_job(client, "http://printer.local/print",
                              make_job(location=None, group="")))
    assert ok is True
    parts = parse_multipart(capture["request"].content,
                            capture["request"].headers["content-type"])
    assert "location" not in parts
    assert "group" not in parts


@pytest.mark.parametrize("status", [201, 204])
def test_send_job_2xx_is_success(status: int):
    client = client_capturing({}, status_code=status)
    assert asyncio.run(send_job(client, "http://p/", make_job())) is True


@pytest.mark.parametrize("status", [400, 404, 500])
def test_send_job_non_2xx_is_failure(caplog, status: int):
    client = client_capturing({}, status_code=status)
    with caplog.at_level(logging.WARNING, logger="pta2ccs.printjob"):
        assert asyncio.run(send_job(client, "http://p/", make_job())) is False
    assert any("rejected job" in rec.message for rec in caplog.records)


def test_send_job_transport_exception_is_failure(caplog):
    client = client_capturing({}, exc=httpx.ConnectError("boom"))
    with caplog.at_level(logging.WARNING, logger="pta2ccs.printjob"):
        assert asyncio.run(send_job(client, "http://p/", make_job())) is False
    assert any("post failed" in rec.message for rec in caplog.records)
