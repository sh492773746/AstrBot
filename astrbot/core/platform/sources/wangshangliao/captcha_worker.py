"""Private stdin/stdout worker; no network listener or credential persistence."""

import importlib.util
import json
import sys
from pathlib import Path


def main() -> None:
    """Reuse one model and one Node worker until the parent closes stdin."""
    spec = importlib.util.spec_from_file_location(
        "wsl_solver_protocol",
        Path(__file__).with_name("_solver_vendor") / "网易_滑块增强版_协议.py",
    )
    protocol = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = protocol
    spec.loader.exec_module(protocol)
    try:
        recognizer = protocol.load_recognizer(worker_count=1)
        for line in sys.stdin:
            try:
                request = json.loads(line)
                run = protocol.solve_once(
                    captcha_id=request["id"],
                    referer=request["referer"],
                    fp="",
                    width=320,
                    save_debug=False,
                    render_overlay=False,
                    refresh_fp=False,
                    allow_intellisense_precheck=True,
                    local_fp_workers=1,
                    recognizer=recognizer,
                )
                validate = run.get("onVerify", {}).get("validate", "")
                reply = {"validate": validate}
            except Exception as exc:
                reply = {"validate": "", "error": type(exc).__name__}
            print(json.dumps(reply), flush=True)
    finally:
        protocol.shutdown_tracked_child_processes()


if __name__ == "__main__":
    main()
