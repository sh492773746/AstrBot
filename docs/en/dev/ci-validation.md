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

CodeQL findings are fixed in application code, not hidden by query exclusions,
alert dismissal or weaker branch policies. Path access must use a normalized,
directory-confined path, including when a symlink exists.

AI-avatar credential verifier changes require special care for already paid
remote tasks. See the plugin README's credential verifier maintenance section:
legacy task IDs and quota reservations are preserved for explicit owner review,
and are never automatically resubmitted.
