"""MetaMediaUploader — Meta's two-step Resumable Upload API → the template media handle.

The one question: does it run start-session then upload-bytes correctly and read back the `h` handle,
so a carousel card's header_example_handle can be filled without the caller touching Meta directly?
Verified shapes (Sep 2026): POST /{app}/uploads?file_length&file_type → {"id":"upload:.."}; then
POST /{session} with Authorization: OAuth + file_offset:0 + bytes → {"h":".."}.
"""
from __future__ import annotations

import json

from app.adapters.whatsapp_media import meta as meta_mod
from app.adapters.whatsapp_media.console import ConsoleMediaUploader
from app.adapters.whatsapp_media.meta import MetaMediaUploader

_BASE = "https://graph.facebook.test/v20.0"


def test_meta_uploader_runs_both_steps_and_returns_the_handle(monkeypatch):
    calls = []

    def fake_post_json(url, payload, headers=None):
        calls.append(("post_json", url))
        return (200, json.dumps({"id": "upload:sess1"}))

    def fake_request(url, data, headers, method="POST"):
        calls.append(("request", url, headers.get("Authorization"), headers.get("file_offset"), data))
        return (200, json.dumps({"h": "HANDLE123"}))

    monkeypatch.setattr(meta_mod, "post_json", fake_post_json)
    monkeypatch.setattr(meta_mod, "_request", fake_request)
    r = MetaMediaUploader("tok", "APP1", _BASE).upload_handle(
        file_bytes=b"\xff\xd8imagebytes", file_type="image/jpeg", file_name="hero.jpg")
    assert r.ok and r.handle == "HANDLE123"
    # step 1 hits /{app}/uploads with the params; step 2 posts the bytes to the session with OAuth+offset
    assert calls[0][1].startswith(f"{_BASE}/APP1/uploads?") and "file_type=image%2Fjpeg" in calls[0][1]
    assert calls[1][1] == f"{_BASE}/upload:sess1"
    assert calls[1][2] == "OAuth tok" and calls[1][3] == "0" and calls[1][4] == b"\xff\xd8imagebytes"


def test_meta_uploader_reports_a_start_failure(monkeypatch):
    monkeypatch.setattr(meta_mod, "post_json",
                        lambda u, p, h=None: (400, json.dumps({"error": {"message": "bad app"}})))
    r = MetaMediaUploader("tok", "APP1", _BASE).upload_handle(file_bytes=b"x", file_type="image/png")
    assert r.ok is False and "bad app" in r.reason


def test_meta_uploader_reports_a_bytes_failure(monkeypatch):
    monkeypatch.setattr(meta_mod, "post_json", lambda u, p, h=None: (200, json.dumps({"id": "upload:s"})))
    monkeypatch.setattr(meta_mod, "_request",
                        lambda u, d, h, method="POST": (500, json.dumps({"error": {"message": "boom"}})))
    r = MetaMediaUploader("tok", "APP1", _BASE).upload_handle(file_bytes=b"x", file_type="image/png")
    assert r.ok is False and "boom" in r.reason


def test_meta_uploader_refuses_empty_bytes():
    assert MetaMediaUploader("tok", "APP1", _BASE).upload_handle(
        file_bytes=b"", file_type="image/png").ok is False


def test_console_uploader_returns_a_stable_handle():
    up = ConsoleMediaUploader()
    a = up.upload_handle(file_bytes=b"same", file_type="image/png")
    b = up.upload_handle(file_bytes=b"same", file_type="image/png")
    assert a.ok and a.handle == b.handle and a.handle.startswith("console-handle:")


def test_meta_upload_media_id_posts_multipart_to_the_phone_number(monkeypatch):
    seen = {}

    def fake_request(url, data, headers, method="POST"):
        seen["url"], seen["ctype"], seen["data"] = url, headers.get("Content-Type"), data
        return (200, json.dumps({"id": "MEDIA_9"}))

    monkeypatch.setattr(meta_mod, "_request", fake_request)
    r = MetaMediaUploader("tok", "APP1", _BASE).upload_media_id(
        file_bytes=b"imgbytes", file_type="image/png", phone_number_id="PN1", file_name="a.png")
    assert r.ok and r.media_id == "MEDIA_9"
    assert seen["url"] == f"{_BASE}/PN1/media"
    assert seen["ctype"].startswith("multipart/form-data; boundary=")
    # the multipart body carries messaging_product + the file bytes
    assert b"messaging_product" in seen["data"] and b"imgbytes" in seen["data"]


def test_meta_upload_media_id_needs_a_phone_number():
    r = MetaMediaUploader("tok", "APP1", _BASE).upload_media_id(
        file_bytes=b"x", file_type="image/png", phone_number_id="")
    assert r.ok is False


def test_console_upload_media_id_returns_a_stable_id():
    r = ConsoleMediaUploader().upload_media_id(file_bytes=b"x", file_type="image/png",
                                               phone_number_id="PN1")
    assert r.ok and r.media_id.startswith("console-media:")
