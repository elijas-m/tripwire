"""DB maintenance regressions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tripwire.config import Config
from tripwire.db import DB


class SecretIsolationTests(unittest.TestCase):
    def test_tests_never_load_live_secrets(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.secrets, {})


class VacuumTests(unittest.IsolatedAsyncioTestCase):
    async def test_vacuum_after_select_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(Path(tmp) / "tripwire.db")
            await db.open()
            try:
                await db.fetchone("SELECT 1")
                await db.vacuum()
            finally:
                await db.close()
