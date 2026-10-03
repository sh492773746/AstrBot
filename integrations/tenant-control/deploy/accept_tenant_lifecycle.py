"""Operator-authorized lifecycle acceptance, limited to a known non-paid trial."""

import json
import os
import time
from pathlib import Path

from cryptography.fernet import Fernet
from tenant_control.store import Store
from tenant_control.worker import Worker


def main():
    bot_id = 8852060111
    db = Store(Path("/var/lib/astrbot-tenant-state/control.db"))
    row = db.db.execute(
        "SELECT b.* FROM bots b JOIN trials t ON t.bot_id=b.id "
        "WHERE b.id=? AND b.owner_id=1000000003 AND b.status='active' "
        "AND NOT EXISTS(SELECT 1 FROM orders o WHERE o.bot_id=b.id)",
        (bot_id,),
    ).fetchone()
    if not row:
        raise ValueError("Acceptance is limited to the confirmed non-paid trial")
    key = json.loads(
        (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "worker.json").read_text()
    )["CONTROL_FERNET_KEY"]
    worker = Worker(
        db,
        Fernet(key.encode()),
        Path("/var/lib/astrbot-tenants"),
        Path("/opt/astrbot-runtime/tenant-source"),
        os.environ["ASTRBOT_IMAGE"],
        Path("/opt/astrbot-runtime/tenant-source/provider.json"),
    )
    report = {"bot_id": bot_id, "at": int(time.time()), "checks": {}}

    def audit(action):
        db.db.execute(
            "INSERT INTO audit(at,actor,action,ref) VALUES(?,?,?,?)",
            (int(time.time()), "operator-acceptance", action, str(bot_id)),
        )

    data = worker.root / str(bot_id) / "data"
    marker = data / "acceptance-marker.txt"
    if marker.exists():
        raise ValueError("Review earlier acceptance before retry")
    marker.write_text("before-restore")
    try:
        audit("lifecycle_acceptance_start")
        snapshot = worker.backup(bot_id)
        report["snapshot"] = snapshot.name
        report["checks"]["backup"] = (snapshot / "cmd_config.json").is_file()
        db.set_bot_enabled(bot_id, False, "operator-acceptance")
        worker.reconcile(bot_id)
        report["checks"]["pause"] = worker._state(bot_id) == "stopped"
        db.set_bot_enabled(bot_id, True, "operator-acceptance")
        worker.reconcile(bot_id, wait_health=True)
        report["checks"]["resume"] = worker._healthy(bot_id)
        audit("trial_expiry_simulation")
        db.db.execute(
            "UPDATE bots SET expires_at=? WHERE id=?", (int(time.time()) - 1, bot_id)
        )
        worker.reconcile(bot_id)
        report["checks"]["expired_stopped"] = worker._state(bot_id) == "stopped"
        db.db.execute(
            "UPDATE bots SET expires_at=? WHERE id=?", (row["expires_at"], bot_id)
        )
        worker.reconcile(bot_id, wait_health=True)
        report["checks"]["expiry_restored"] = worker._healthy(bot_id)
        marker.write_text("after-restore")
        worker.restore(bot_id, snapshot.name)
        report["checks"]["restore_content"] = marker.read_text() == "before-restore"
        report["checks"]["restore_health"] = worker._healthy(bot_id)
        upgrade_snapshot = worker.upgrade_plugin(bot_id)
        report["checks"]["upgrade_health"] = worker._healthy(bot_id)
        worker.restore(bot_id, upgrade_snapshot.name)
        report["checks"]["rollback_health"] = worker._healthy(bot_id)
        audit("lifecycle_acceptance_complete")
    finally:
        db.db.execute(
            "UPDATE bots SET expires_at=?,enabled=? WHERE id=?",
            (row["expires_at"], row["enabled"], bot_id),
        )
        marker.unlink(missing_ok=True)
        db.close()
        output = Path("/var/lib/astrbot-tenant-worker/lifecycle-acceptance.json")
        output.write_text(json.dumps(report, indent=2))
        output.chmod(0o600)
        print(json.dumps(report), flush=True)
    if not all(report["checks"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
