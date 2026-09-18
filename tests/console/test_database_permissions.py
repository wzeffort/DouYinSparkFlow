import os
import stat
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine

from spark_console.db import create_schema


@unittest.skipUnless(os.name == 'posix', 'POSIX database permissions; also run in Linux container')
class DatabasePermissionTests(unittest.TestCase):
    def test_existing_database_is_owner_only_without_changing_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'spark.db'
            engine = create_engine(f'sqlite:///{path}')
            try:
                create_schema(engine)
                with engine.begin() as conn:
                    conn.exec_driver_sql('CREATE TABLE audit_fixture (value TEXT)')
                    conn.exec_driver_sql("INSERT INTO audit_fixture VALUES ('preserve')")
                path.chmod(0o644)
                create_schema(engine)
                self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
                with engine.connect() as conn:
                    self.assertEqual('preserve', conn.exec_driver_sql('SELECT value FROM audit_fixture').scalar())
            finally:
                engine.dispose()
