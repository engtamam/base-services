# Phase 46.7 — mutation evidence for the backup edge-case guards

A passing test proves nothing until a mutant proves it can fail. Every edge-case guard the phase
names was mutation-tested: the guard was deliberately broken, the guardian test was run in
isolation, and it had to FAIL (kill the mutant) — otherwise the test measures nothing. All mutants
were killed; each was reverted immediately after.

| # | edge case | mutation applied | guardian test | result |
|---|-----------|------------------|---------------|--------|
| 1 | process dies mid-write (half-write) | `if not sealed or not checksum:` → `if False:` in `backup.verify` | `test_an_archive_missing_its_sealed_body_is_a_clean_refusal_not_a_crash` | **killed** |
| 2 | backup corrupted at rest | `if _checksum(sealed) != checksum:` → `if False:` | `test_corruption_at_rest_is_caught_by_the_checksum` | **killed** |
| 3 | a service down during a run | `TargetUnreachable → MISSED` → `→ OK` in `orchestrator._capture_one` | `test_a_service_down_during_a_run_is_missed_and_makes_the_run_incomplete` | **killed** |
| 4 | KEK rotated / mismatched | the decrypt-failure `raise BackupError` → swallow to `{"collections": {}}` in `import_archive` | `test_a_restore_with_the_wrong_backup_key_is_refused` | **killed** |
| 5 | restore into a partly-existing tenant / re-run | `elif overwrite:` → `elif True:` (always clobber) in `import_archive` | `test_a_restore_re_run_changes_nothing_and_does_not_clobber_newer_rows` | **killed** |
| 6 | storage full mid-run | revert the try/except around `object_store.put` so it aborts the whole run | `test_storage_full_fails_one_target_but_not_the_whole_run` | **killed** |

Mutant #1 first SURVIVED: the original test deleted `checksum`, which the checksum-comparison branch
also catches, so the missing-body guard was not isolated. The test was rewritten to a `None` sealed
body — which without the guard crashes with `AttributeError` instead of a clean `BackupError` — and
the mutant was then killed. That is the mutation loop working as intended.

Mutant #6 is the regression guard for the bug fixed in 46.7: `object_store.put` was unwrapped, so a
storage-full error aborted the entire run and denied every other target its backup. It is now caught
per-target (FAILED), and the mutant that reverts the fix is killed.

Reproduce: `python <scratchpad>/mutate_467.py` (the harness saves each file, applies one mutation,
runs the single guardian test, and restores the file byte-for-byte).
