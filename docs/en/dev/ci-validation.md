# CI Validation

The complete Python suite runs in four deterministic module shards on each of
Linux, macOS and Windows. Windows failures are blocking. No profile, test marker
or existing test file is excluded to obtain a passing result. Each shard
archives its selected node IDs, JUnit report and console log. Final jobs reject
missing, overlapping or inconsistent collection manifests.

Coverage runs the same complete collection in four bounded Linux workers.
Only after all workers pass and their manifests cover the collection exactly
once are their coverage databases combined into the final XML report.
Cancellation or timeout is a failure, not a successful coverage run.

To reproduce a shard after installing development and WSL dependencies:

```bash
ASTRBOT_CI_SHARD_INDEX=0 ASTRBOT_CI_SHARD_COUNT=4 \
  uv run python scripts/ci_pytest.py tests -q
```

Omit both shard variables for the complete suite in one process. The runner
keeps its watchdog active through output flushing and returns pytest's actual
exit status even if tests leave non-daemon workers alive.

Offline socket guards are installed after the asyncio loop starts, including
on Windows where the loop's wakeup pair uses TCP. They still reject loopback
connections initiated by the test body. Large binary parameters use short
case IDs or stable length/digest labels, without replacing the input payloads.
This keeps `PYTEST_CURRENT_TEST` within Windows limits and prevents megabyte
log lines from stalling the hosted runner's log transport.
Permission tests verify the private-mode calls on every OS and actual POSIX
mode bits on Unix; this does not assert that Windows ACLs equal Unix modes.
Native permission refresh tests use a fixed clock to ensure a sequential
forced read cannot be mistaken for a concurrent coalesced read.
The dashboard finance-window lock regression rejects recursive acquisition
directly and exercises an injected shared-lock failure. It does not infer a
deadlock from the time taken to initialize SQLite on a hosted runner.

CodeQL findings are fixed in application code, not hidden by query exclusions,
alert dismissal or weaker branch policies. Path access must use a normalized,
directory-confined path, including when a symlink exists.
Deletion confines the resolved parent but preserves the selected directory
entry, so removing a symlink never deletes its target. Configuration and
persistent data are inspected for cleanup only when their deletion is selected.

AI-avatar credential verifier changes require special care for already paid
remote tasks. See the plugin README's credential verifier maintenance section:
legacy task IDs and quota reservations are preserved for explicit owner review,
and are never automatically resubmitted.
