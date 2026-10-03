"""Migration helper checks run against temp files, never running services."""

import json
import runpy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

MODULE = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "deploy/unify_controller.py")
)


class MigrationTests(unittest.TestCase):
    def test_sqlite_snapshot_preserves_wal_and_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, target = root / "source.db", root / "target.db"
            with sqlite3.connect(source) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE audit(ref TEXT)")
                connection.execute("INSERT INTO audit VALUES('preserved')")
                connection.commit()
                MODULE["backup_db"](source, target)
            with sqlite3.connect(target) as db:
                self.assertEqual(
                    db.execute("SELECT ref FROM audit").fetchone()[0], "preserved"
                )
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_management_api_rejects_application_error_without_printing_secrets(self):
        client = MagicMock()
        client.request.return_value.status_code = 200
        client.request.return_value.json.return_value = {
            "status": "error",
            "message": "secret must not be echoed",
        }
        with self.assertRaises(RuntimeError) as error:
            MODULE["api"](client, "POST", "/test")
        self.assertNotIn("secret", str(error.exception))

    def test_config_writer_is_private_and_readable_with_bom(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            MODULE["save"](path, {"enabled": False})
            self.assertEqual(MODULE["load"](path), {"enabled": False})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text("\ufeff" + json.dumps({"enabled": True}))
            self.assertTrue(MODULE["load"](path)["enabled"])
