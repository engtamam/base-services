"""A CronJob schedule means nothing without the zone it is read in.

Without spec.timeZone a CronJob is timed by whatever zone the NODE keeps, so a sweep written for
09:00 Riyadh fires at 04:00 on an Asia/Shanghai node — every hour reasoned about in a values file is
silently wrong by the node's offset. These tests prove the chart:

  * applies the deployment's timeZone to every CronJob,
  * lets one job override it,
  * REJECTS an unknown zone at TEMPLATE time (not at the scheduler tick), against the IANA list
    bundled in the chart, and
  * lets an operator opt into the node zone deliberately with "".

Install helm, or point HELM_BIN at the binary.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "chart"
TZ_FILE = CHART / "timezones.txt"

HELM = shutil.which("helm") or os.environ.get("HELM_BIN")
pytestmark = pytest.mark.skipif(not HELM, reason="helm not installed — chart guards cannot run")

_CRON = [{"name": "digest", "schedule": "0 9 * * *", "path": "/internal/notify/digest"}]
BASE = [
    "--set", "service.name=notification-service",
    "--set", "image.repository=ghcr.io/bs/notification",
    "--set", "image.digest=sha256:" + "a" * 64,
    "--set", "existingSecret=app-secret",
    "--set-json", f"cronJobs={json.dumps(_CRON)}",
]


def _run(*extra: str):
    return subprocess.run([HELM, "template", "t", str(CHART)] + BASE + list(extra),
                          capture_output=True, text=True)


def _cronjob(*extra: str) -> dict:
    proc = _run(*extra)
    assert proc.returncode == 0, f"render failed:\n{proc.stderr}"
    for d in yaml.safe_load_all(proc.stdout):
        if d and d.get("kind") == "CronJob":
            return d
    raise AssertionError("no CronJob rendered")


def test_the_deployment_timezone_is_applied_to_every_cronjob():
    # The chart default is the platform's home market; every schedule is read in it.
    assert _cronjob().get("spec", {}).get("timeZone") == "Asia/Riyadh"
    assert _cronjob("--set", "timeZone=Europe/London")["spec"]["timeZone"] == "Europe/London"


def test_a_cronjob_can_override_the_deployment_timezone():
    cron = [{**_CRON[0], "timeZone": "UTC"}]
    cj = _cronjob("--set-json", f"cronJobs={json.dumps(cron)}", "--set", "timeZone=Asia/Riyadh")
    assert cj["spec"]["timeZone"] == "UTC"                 # the job's own zone wins over the default


def test_an_unknown_timezone_is_rejected_at_template_time():
    proc = _run("--set", "timeZone=Asia/Nowhere")
    assert proc.returncode != 0, f"expected a render ERROR, got:\n{proc.stdout[:300]}"
    assert "not a known IANA zone" in proc.stderr and "Asia/Nowhere" in proc.stderr


def test_empty_timezone_opts_into_the_node_zone_deliberately():
    # "" is the pre-timeZone behaviour, but now a CHOICE on the record — no spec.timeZone is emitted.
    assert "timeZone" not in _cronjob("--set", "timeZone=").get("spec", {})


def test_the_bundled_zone_list_is_sane_and_lf_only():
    raw = TZ_FILE.read_bytes()
    assert b"\r" not in raw, "timezones.txt must be LF-only or helm's splitList mismatches every zone"
    zones = TZ_FILE.read_text(encoding="utf-8").split("\n")
    zones = [z for z in zones if z]
    assert len(zones) > 500                                # the real IANA set, not a stub
    assert zones == sorted(zones)                          # sorted → deterministic membership
    for must in ("Asia/Riyadh", "UTC", "Europe/London", "America/New_York"):
        assert must in zones


def test_every_deployment_timezone_in_the_repo_is_renderable():
    # Any timeZone an actual values file sets must be in the bundled list, or that deployment cannot
    # render — the practical guard that keeps the list and the fleet in step.
    zones = set(TZ_FILE.read_text(encoding="utf-8").split("\n"))
    for vf in sorted((ROOT / "deploy" / "services").glob("*.values.yaml")):
        data = yaml.safe_load(vf.read_text(encoding="utf-8")) or {}
        tz = data.get("timeZone")
        if tz:
            assert tz in zones, f"{vf.name} sets timeZone {tz!r} which is not in timezones.txt"
        for job in (data.get("cronJobs") or []):
            jz = job.get("timeZone")
            if jz:
                assert jz in zones, f"{vf.name} cronJob {job.get('name')!r} timeZone {jz!r} not bundled"
