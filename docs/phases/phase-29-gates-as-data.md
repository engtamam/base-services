# Phase 29 — Gates as data: a runner that fits any repository

Triggered by an adopter memo. They wanted our gate runner. They could not use it, so
they copied it — two hundred lines that will drift within a month. Their words: *"That is a sign
it should be general."*

They were right, and the proof is worse than their diagnosis. Pointed at a repository with no
`services/` directory, our runner printed:

```
  PASS  suites
  PASS  security-gate
RESULT: ALL RUNNABLE GATES PASSED.
```

Zero tests. Zero scanners. Exit 0. **A loop over an empty set passes, loudly, in green.** That is
the defect every gate in this repo exists to catch, sitting inside the thing that catches them.
It is not their problem: rename `services/` here, or drop one `app/` marker, and our merge gate
goes green while guarding nothing.

---

## 1. Definition of Done

### (1) Is this on the hot path?

The gate runner *is* the hot path of the development loop — every push, every merge, every
release. Two gates were serial `for` loops and cost 7½ minutes between them. They now fan out:
`suites` 3m30s → 42s, `security-gate` ~4min → 67s.

But parallelism over a **shared external resource** is where this phase's real footgun lives, and
we have now hit it four times:

| Shared thing | Symptom | Actually |
| --- | --- | --- |
| One postgres, two pytest processes | "adapter divergence" | concurrent `TRUNCATE` |
| One postgres, 18 parallel suites | 14 services failing | concurrent `TRUNCATE` |
| One PyPI advisory DB, 18 `pip-audit` calls | "CVE found" | a malformed HTTP body |
| One `/tmp/gate_<svc>.log`, two runs | 14 services failing | overwritten logs |

Every time the symptom accused the code and the cause was the harness. The rule this phase
encodes: **a gate that fans out must declare what it shares.** `security-gate` is capped at 4
because `pip-audit` is network-bound; `contracts:postgres` stays serial because it owns a
database; `suites` uses all cores because it shares nothing — and it must therefore have its
`DATASTORE_TEST_DSN` cleared, or it silently shares one.

### (2) Does every config knob have a consumer?

`GATE_UNITS_DIR`, `GATE_UNIT_MARKER`, `GATE_UNIT_SKIP`, `GATE_GATES_DIR` are consumed by
`services()` and the gate loader, and each is exercised by `tools/ci/tests/`. A knob that only
appears in documentation is the exact defect `coverage:services` exists to catch.

### (3) What does production need that the memo omits?

* **An empty discovery must be fatal.** The memo says the runner "finds nothing and passes
  green" as though that were a usability problem. It is a correctness bug, and it is the first
  thing fixed here.
* **A gate must be provable.** Discovery alone does not stop anyone shipping a gate that cannot
  fail. `gates.d/` makes gates cheap to add — which makes decoration cheap to add. So a gate
  file may declare `gate_selftest`, a command that must FAIL on a known-bad input. `--verify`
  runs every selftest and reports which gates have none.
* **`--list` must be generated, not written.** The memo asks parity to check three places:
  described, implemented, run. We check two, and delete the third — a gate's description lives
  in the gate file, beside its command, so "implemented but undescribed" becomes unrepresentable.
  *Do not gate a class of bug you can make impossible.*

### (4) The correctness footgun

**`SKIP` and `PENDING` are different facts, and conflating them corrodes the outcome that
matters.** This is the memo's sharpest point, and we have the scar tissue to prove it: our
`container (trivy)` gate skipped silently for months behind a green board, because nobody
distinguished "the scanner is missing" from "there is nothing to scan".

```
PASS      the gate ran and held
FAIL      the gate ran and caught something        -> merge blocked
SKIP      the gate is wired, its INFRA is absent   -> --strict makes it a FAIL
PENDING   the gate is wired, its SUBJECT is not built yet
                                                   -> listed loudly, never blocks
```

A gate wired before the code it guards exists is a *promise*. A gate whose tool is missing is a
*hole*. Failing CI for the first teaches the team that `--strict` is noise — and then a real
`SKIP` gets waved through.

---

## 2. The gate file

One file per gate in `tools/ci/gates.d/`, ordered by filename. Sourced in a subshell, so a gate
cannot corrupt the runner or another gate.

```sh
gate_name="safety:scans"
gate_desc="no unexplained full-collection read on a request path"
gate_protects="Every surviving scan_all() must say, in a '# sweeper:' note, why
reading every row IS the operation."

gate_needs="python"                 # commands; missing -> SKIP
gate_env=""                         # env vars that must be set; missing -> SKIP
gate_subject="tools/ci/check_scan_discipline.py"   # missing -> PENDING
gate_units=0                        # 1 = needs a non-empty discovery
gate_slow=0                         # 1 = dropped by --profile quick

gate_main() { "$PY" tools/ci/check_scan_discipline.py; }

# Optional, and the thing that keeps a gate honest: a command that must FAIL.
gate_selftest() { ! "$PY" tools/ci/check_scan_discipline.py --selftest-should-fail; }
```

Their gates live in their own `gates.d/`. Ours live in ours. Nobody edits the runner.

## 3. Steps

| # | Step |
| --- | --- |
| 29.1 | `require_units` — an empty discovery is FATAL, wired into every per-unit gate ✅ |
| 29.2 | Discovery is config: `GATE_UNITS_DIR` / `GATE_UNIT_MARKER` / `GATE_UNIT_SKIP` ✅ |
| 29.3 | `tools/ci/gatelib.sh` — outcomes, discovery, `fan_out`, `py_for`, `require_units` |
| 29.4 | `gates.d/` loader; `--list` generated from gate metadata |
| 29.5 | `PENDING` as a fourth outcome, plumbed through `--strict` and the summary |
| 29.6 | Port all 12 gates into `gates.d/`, each mutation-tested |
| 29.7 | `check_ci_parity.py` reads `gates.d/` instead of parsing a heredoc |
| 29.8 | `--verify`: run every `gate_selftest`; report gates that have none |
| 29.9 | `.github/workflows/gates.yml` — `workflow_call`, so a downstream repo's CI is three lines |
| 29.10 | Tests: empty discovery fails; foreign layout discovers; PENDING never blocks; SKIP does under strict |

## 4. Out of scope

Rewriting the individual checks. `check_scan_discipline.py`, `check_product_config.py` and the
per-service `ci/security_gate.py` are unchanged; only how they are *declared and discovered*
changes.

## 5. Credit

The design came from an adopter team. Three of their four proposals are adopted as written. The fourth — parity
across three places — is adopted by removing the third place instead of checking it.

**Where their patch was wrong.** They also reported that our `MIGRATION:` exemption was a
substring check, which was true and which we had shipped without seeing. Their proposed fix:

```python
return any(l.lstrip().startswith(MIGRATION_MARKER) for l in body.splitlines())
```

`lstrip()` re-opens the hole it closes. Measured against the five cases:

| commit body | theirs | ours |
| --- | --- | --- |
| `requires a MIGRATION: line in the body` (prose) | block | block |
| `    MIGRATION: indented, so quoted prose` | **ALLOW** | block |
| `MIGRATION:` (no reason) | **ALLOW** | block |
| `MIGRATION: x` (reason too short) | **ALLOW** | block |
| `MIGRATION: rotate signatureDomain .v2, 90-day window` | allow | allow |

Indented prose is how everyone quotes a rule — including the commit that documents it. Their
diagnosis found a bug we could not see; their patch would have reintroduced it in six months.

**And their warning we could not have found.** `git grep -Il $'\r'` inside `$(...)`: command
substitution strips the carriage return, the pattern becomes empty, and an empty pattern matches
**every file**. Verified in a scratch repo — it returned all four files, including the LF-only
ones. `git ls-files --eol` reports only the CRLF file. We ship no CRLF gate today; when we do, it
uses `git ls-files --eol`.

---

**STATUS: 🟢 COMPLETE (29.1–29.10).**

`run_all_gates.sh` went from 560 lines containing twelve gates to ~230 lines containing none. The
thirteen gates live in `tools/ci/gates.d/*.gate`. Point the runner at another repository, pass
`GATE_GATES_DIR` and `GATE_UNITS_DIR`, and it gates that repository. Their CI becomes three
lines calling `.github/workflows/gates.yml`.

**The parser I wrote to fix the bug reintroduced the bug.** `meta()` matched only *quoted*
values, so `gate_units=1` read as the empty string, every per-unit gate silently skipped
`require_units`, and a run against a repo with no units passed green again. Caught by writing the
test before believing the fix. A metadata reader that returns `""` for a value it cannot parse is
a metadata reader that lies — the same defect, three levels deep:

1. a gate that iterates an empty set passes;
2. the guard against that is disabled by a parser that reads its flag as empty;
3. nothing tested the parser.

`tools/ci/tests/test_gate_runner.py` (11 tests) closes all three. The new `safety:runner` gate
runs them **first**, before the runner judges anything else — and it also runs `scripts/tests/`,
which had existed for months while **no gate ever ran it**.

`--verify` executes each gate's `gate_selftest` against a known-bad input and reports which gates
have never been seen to fail. Two are proven today; eleven are debt, named out loud rather than
assumed.

**On Windows, `bash` resolves to WSL**, which cannot execute a script at a `C:\` path. It does not
fail loudly: stderr says `execvpe(/bin/bash) failed` and stdout is empty, which reads exactly like
a runner that produced no output. The runner's tests locate Git Bash explicitly.
