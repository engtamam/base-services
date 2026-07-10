#!/usr/bin/env python
"""Run every service's tests CONCURRENTLY, optionally against a real PostgreSQL.

    python scripts/test_parallel.py                 # unit + integration + security
    python scripts/test_parallel.py --pg            # ...plus contracts on a real postgres
    python scripts/test_parallel.py --pg --only billing-service
    python scripts/test_parallel.py --pg --keep     # leave the container up to poke at it

Two things make the parallel run HONEST rather than merely fast. Both were learned by getting
them wrong first, and the wrong version does not announce itself — it reports failures that look
like adapter bugs:

1. ONE DATABASE PER SERVICE. The contract fixture runs `TRUNCATE kv_store` on setup. Point two
   services at one database and the first one's TRUNCATE deletes the second one's rows while it
   is mid-test. Chasing those "divergences" costs a day.

2. A RAISED CONNECTION LIMIT ON THE THROWAWAY CONTAINER. 18 services x asyncpg's default pool
   (max 10) = 180 connections; stock postgres allows 100 and the run dies with "sorry, too many
   clients already". The limit is raised HERE, on a container that lives for the length of this
   script. Nothing in the adapter, the chart, or any deploy value is touched — a test harness
   must never make production hold its breath.

Exit code is non-zero if any service fails. Logs land in .test-logs/.
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import os
import pathlib
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOGS = ROOT / ".test-logs"

CONTAINER = os.environ.get("PG_CONTAINER", "bs-pg-test")
PG_PORT = os.environ.get("PG_PORT", "55432")
PG_IMAGE = os.environ.get("PG_IMAGE", "postgres:16-alpine")
# 18 services x asyncpg's default pool of 10, plus headroom. Test container only.
PG_MAX_CONNECTIONS = os.environ.get("PG_MAX_CONNECTIONS", "400")

SUITES = ["tests/unit", "tests/integration", "tests/security"]

# The skip reason the contract fixture uses when no DSN reaches it. Matching the REASON, not the
# word "skipped", keeps unrelated infra-gated skips (no redis, no S3) from crying wolf.
_PG_SKIP_MARKER = "set DATASTORE_TEST_DSN to run the postgres contract"


def python_exe() -> str:
    for candidate in (ROOT / ".venv-test/Scripts/python.exe", ROOT / ".venv-test/bin/python"):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def services(only: str | None) -> list[str]:
    out = []
    for path in sorted((ROOT / "services").iterdir()):
        if not (path / "tests").is_dir():
            continue
        if only and path.name != only:
            continue
        out.append(path.name)
    return out


def db_name(service: str) -> str:
    return "db_" + service.replace("-", "_")


def docker(*args: str, check: bool = True, quiet: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=check,
                          stdout=subprocess.DEVNULL if quiet else None,
                          stderr=subprocess.DEVNULL if quiet else None)


def psql(sql: str) -> int:
    return subprocess.run(
        ["docker", "exec", CONTAINER, "psql", "-U", "pg", "-d", "bs", "-q", "-c", sql],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def pg_ready() -> bool:
    return subprocess.run(["docker", "exec", CONTAINER, "pg_isready", "-U", "pg", "-d", "bs"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def start_pg(names: list[str]) -> None:
    if not shutil.which("docker"):
        sys.exit("docker not found — drop --pg")
    print(f"==> postgres: {PG_IMAGE} on :{PG_PORT} "
          f"(max_connections={PG_MAX_CONNECTIONS}, throwaway container)")
    docker("rm", "-f", CONTAINER, check=False)
    docker("run", "-d", "--name", CONTAINER,
           "-e", "POSTGRES_PASSWORD=pg", "-e", "POSTGRES_USER=pg", "-e", "POSTGRES_DB=bs",
           "-p", f"{PG_PORT}:5432", PG_IMAGE, "-c", f"max_connections={PG_MAX_CONNECTIONS}")

    # Wait for STABLE readiness. During initdb the image runs a temporary server, answers
    # pg_isready, then shuts it down and starts the real one. A loop that stops at the first
    # success races that restart and talks to a database about to disappear.
    consecutive = 0
    for _ in range(90):
        consecutive = consecutive + 1 if pg_ready() else 0
        if consecutive >= 3:
            break
        time.sleep(1)
    else:
        docker("logs", "--tail", "20", CONTAINER, check=False, quiet=False)
        sys.exit("postgres never became ready")

    for name in names:                       # one database per service — see the module docstring
        psql(f"DROP DATABASE IF EXISTS {db_name(name)};")
        psql(f"CREATE DATABASE {db_name(name)};")


def run_service(name: str, suites: list[str], use_pg: bool) -> tuple[str, int, str]:
    env = dict(os.environ, PYTHONHASHSEED="0")
    env["DATASTORE_TEST_DSN"] = (
        f"postgresql://pg:pg@127.0.0.1:{PG_PORT}/{db_name(name)}" if use_pg else "")
    log = LOGS / f"{name}.log"
    with log.open("w", encoding="utf-8") as fh:
        rc = subprocess.run([python_exe(), "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", *suites],
                            cwd=ROOT / "services" / name, env=env,
                            stdout=fh, stderr=subprocess.STDOUT).returncode
    text = log.read_text(encoding="utf-8", errors="replace")
    summary = next((ln for ln in reversed(text.splitlines()) if ln.strip()), "")
    return name, rc, summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg", action="store_true", help="also run tests/contracts against a real postgres")
    ap.add_argument("--only", metavar="SERVICE")
    ap.add_argument("--keep", action="store_true", help="leave the postgres container running")
    args = ap.parse_args()

    names = services(args.only)
    if not names:
        sys.exit(f"no services matched {args.only!r}")

    suites = SUITES + (["tests/contracts"] if args.pg else [])
    if args.pg:
        start_pg(names)

    LOGS.mkdir(exist_ok=True)
    for old in LOGS.glob("*.log"):
        try:
            old.unlink()
        except OSError:      # Windows: a previous run still holds the handle. It will be overwritten.
            pass

    print(f"==> {len(names)} services, concurrently")
    started = time.monotonic()
    results: list[tuple[str, int, str]] = []
    try:
        with futures.ThreadPoolExecutor(max_workers=len(names)) as pool:
            for result in pool.map(lambda n: run_service(n, suites, args.pg), names):
                results.append(result)
    finally:
        if args.pg and not args.keep:
            docker("rm", "-f", CONTAINER, check=False)
        elif args.pg:
            print(f"==> leaving {CONTAINER} up (--keep)")

    elapsed = time.monotonic() - started
    print()
    failed = 0
    for name, rc, summary in results:
        if rc == 0:
            print(f"  {name:<24} {summary}")
        else:
            failed += 1
            print(f"  {name:<24} FAIL  (.test-logs/{name}.log)")
            tail = (LOGS / f"{name}.log").read_text(encoding="utf-8", errors="replace").splitlines()
            for line in tail[-6:]:
                print(f"        {line}")

    # A postgres param that SKIPS is a contract nobody verified. Say so, loudly — but only for
    # THAT skip. Services legitimately skip other infra-gated tests (no redis, no S3), and a
    # blunt "any skip fails" check just teaches people to ignore the check.
    if args.pg:
        blind = [n for n, _, _ in results
                 if _PG_SKIP_MARKER in (LOGS / f"{n}.log").read_text(encoding="utf-8",
                                                                     errors="replace")]
        if blind:
            print(f"\n  postgres contract SKIPPED in: {', '.join(blind)} "
                  f"— DATASTORE_TEST_DSN is not reaching them")
            failed += 1

    print(f"\n{'FAIL' if failed else 'PASS'} in {elapsed:.0f}s"
          f"{f' — {failed} service(s) failed' if failed else ''}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
