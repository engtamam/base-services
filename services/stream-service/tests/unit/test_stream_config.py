"""stream-service config — parsing helpers + production boot guards."""
from __future__ import annotations

import pytest

from app.config import Settings

_OK = {"INTERNAL_SERVICE_TOKEN": "x" * 40}


def test_list_helpers_parse():
    s = Settings(SESSION_DEFAULT_OUTPUTS="hls, record ,snapshot",
                 STREAM_ALLOWED_HOSTS="cam.example.com, 10.0.0.0/8",
                 TURN_URLS="turn:a:3478, turn:b:3478", **_OK)
    assert s.default_outputs == ["hls", "record", "snapshot"]
    assert "cam.example.com" in s.stream_allowed_hosts and "10.0.0.0/8" in s.stream_allowed_hosts
    assert s.turn_urls == ["turn:a:3478", "turn:b:3478"]


def test_dev_defaults_boot_with_zero_infra():
    # null engine + no seal key is fine OUTSIDE production (zero-infra boot/test).
    Settings(ENV="test", **_OK).assert_stream_config_ok()


@pytest.mark.parametrize("over, msg", [
    ({"STREAM_ENGINE_DRIVER": "null"}, "null cannot serve"),
    ({"STREAM_ENGINE_DRIVER": "ffmpeg", "SEAL_KEY": ""}, "SEAL_KEY is required"),
    ({"STREAM_ENGINE_DRIVER": "ffmpeg", "SEAL_KEY": "k" * 32,
      "RECORDING_SINK_DRIVER": "file-service", "RECORDING_FILE_SERVICE_URL": ""},
     "RECORDING_FILE_SERVICE_URL is required"),
    ({"STREAM_ENGINE_DRIVER": "ffmpeg", "SEAL_KEY": "k" * 32,
      "RECORDING_SINK_DRIVER": "s3", "WEBRTC_ENABLED": True,
      "MEDIA_EGRESS_DRIVER": "turn", "TURN_URLS": ""}, "requires TURN_URLS"),
])
def test_production_boot_guards_fail_closed(over, msg):
    s = Settings(ENV="production", **{**_OK, **over})
    with pytest.raises(ValueError, match=msg):
        s.assert_stream_config_ok()


def test_production_happy_config_passes():
    Settings(ENV="production", STREAM_ENGINE_DRIVER="ffmpeg", SEAL_KEY="k" * 32,
             RECORDING_SINK_DRIVER="s3", **_OK).assert_stream_config_ok()
