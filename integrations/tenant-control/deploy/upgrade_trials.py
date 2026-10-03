"""Targeted v0.5.0 plugin rollout; no controller migration or worker startup."""

import os
import runpy
import time
from pathlib import Path


def main():
    os.umask(0o077)
    helpers = runpy.run_path(str(Path(__file__).with_name("unify_controller.py")))
    base, prod, plugin = (helpers[k] for k in ("BASE", "PROD", "PLUGIN"))
    load, save, api = (helpers[k] for k in ("load", "save", "api"))
    if load(base / "private/runtime.json").get("CONTROL_DB_GROUP"):
        raise RuntimeError(
            "This historical v0.5.0 upgrade predates the shared gateway DB"
        )
    canonical = Path(__file__).resolve().parents[1]
    archive = canonical / "dist/astrbot_plugin_tenant_control-v0.5.0.zip"
    previous = canonical / "dist/astrbot_plugin_tenant_control-v0.4.0.zip"
    if not archive.is_file() or not previous.is_file():
        raise RuntimeError("Both reviewed plugin bundles must exist")
    baseline = base / "private" / f"trial-upgrade-{time.time_ns()}"
    baseline.mkdir(mode=0o700)
    config = load(prod / f"data/config/{plugin}_config.json")
    save(baseline / "plugin.json", config)
    runtime = load(base / "private/runtime.json")
    helpers["backup_db"](Path(runtime["CONTROL_DB_PATH"]), baseline / "control.db")
    with helpers["api_client"]() as client:

        def install(path):
            with path.open("rb") as stream:
                api(
                    client,
                    "POST",
                    "/api/plugin/install-upload",
                    files={"file": (path.name, stream, "application/zip")},
                )

        try:
            install(archive)
            updated = dict(
                config,
                allow_trial_grants=True,
                allow_test_purchase=False,
                default_model_quota=0,
            )
            api(
                client,
                "POST",
                "/api/config/plugin/update",
                params={"plugin_name": plugin},
                json=updated,
            )
            for _ in range(45):
                status = base / "runtime-status.json"
                if status.exists():
                    info = load(status)
                    if (
                        info.get("version") == "v0.5.0"
                        and info.get("ready")
                        and time.time() - info["at"] < 40
                    ):
                        save(baseline / "result.json", {"state": "complete"})
                        print(
                            "v0.5.0 ready; trial grants enabled; purchases closed; model quota zero."
                        )
                        return
                time.sleep(2)
            raise RuntimeError("New plugin did not become ready")
        except Exception:
            install(previous)
            api(
                client,
                "POST",
                "/api/config/plugin/update",
                params={"plugin_name": plugin},
                json=config,
            )
            save(baseline / "result.json", {"state": "rolled_back"})
            # Preserve all new business records; the schema change is additive.
            raise


if __name__ == "__main__":
    main()
