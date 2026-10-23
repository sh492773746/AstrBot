#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p ./data/plugins ./data/config ./data/temp

export TESTING="${TESTING:-true}"

# Keep backward compatibility with existing test code that reads ZHIPU_API_KEY.
if [[ -n "${OPENAI_API_KEY:-}" && -z "${ZHIPU_API_KEY:-}" ]]; then
  export ZHIPU_API_KEY="$OPENAI_API_KEY"
fi

PYTEST_TARGETS=("${@:-./tests}")

echo "[ci] syncing dependencies with uv"
uv sync --locked --dev --extra wsl

echo "[ci] installing the browser used by offline plugin UI tests"
uv run python -m playwright install --with-deps chromium --only-shell

echo "[ci] running tests: ${PYTEST_TARGETS[*]}"
# Some tests may leave non-daemon worker threads alive (e.g. aiosqlite warning path),
# which can block pytest process exit in CI. Run pytest via python and force process exit
# with pytest's return code to avoid hanging workflow jobs.
uv run python - "${PYTEST_TARGETS[@]}" <<'PY'
import os
import sys
import threading

import pytest

# Return a real failure before the hosted job's hard cancellation. Otherwise
# GitHub cannot apply the existing best-effort policy to the Windows lane.
timeout = int(os.environ.get("ASTRBOT_CI_TEST_TIMEOUT_SECONDS", "0"))
timer = None
if timeout > 0:
    stderr_fd = os.dup(2)

    def expire():
        try:
            os.write(stderr_fd, b"[ci] Test process exceeded its time budget; failing.\n")
            if os.name == "nt":
                import subprocess

                # Descendants must not keep the runner's output pipes open.
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(os.getpid())],
                    stdout=stderr_fd,
                    stderr=stderr_fd,
                    timeout=10,
                    check=False,
                )
        finally:
            os._exit(124)

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()

exit_code = int(pytest.main(sys.argv[1:]))
if timer is not None:
    timer.cancel()
    os.close(stderr_fd)
sys.stdout.flush()
sys.stderr.flush()
os._exit(exit_code)
PY
