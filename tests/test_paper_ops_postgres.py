"""Opt-in PostgreSQL 17 probe for the operations schema.

Set ``PAPER_OPS_TEST_DSN`` to an isolated PostgreSQL database.  The test uses a
random schema and rolls back, and never prints connection information.
"""

from __future__ import annotations

import os
import unittest
from uuid import uuid4

from paper_service.migrations.v001_initial import SQL as INITIAL_SQL
from paper_service.migrations.v008_operations import SQL as OPERATIONS_SQL


class PaperOperationsPostgresProbe(unittest.TestCase):
    def test_schema_constraints_and_review_trigger_in_rollback_transaction(self):
        dsn = os.environ.get("PAPER_OPS_TEST_DSN")
        if not dsn:
            self.skipTest("PAPER_OPS_TEST_DSN is not configured")
        try:
            import psycopg
        except ImportError:
            self.skipTest("psycopg is not installed")

        schema = "paper_ops_probe_" + uuid4().hex
        self.assertRegex(schema, r"^[a-z0-9_]+$")
        database = psycopg.connect(dsn)
        try:
            database.execute(f'CREATE SCHEMA "{schema}"')
            database.execute(f'SET LOCAL search_path TO "{schema}"')
            database.execute(INITIAL_SQL)
            database.execute(OPERATIONS_SQL)
            server_version = int(
                database.execute(
                    "SELECT current_setting('server_version_num')::integer"
                ).fetchone()[0]
            )
            self.assertGreaterEqual(server_version, 170000)

            tables = {
                row[0]
                for row in database.execute(
                    """SELECT tablename FROM pg_tables
                       WHERE schemaname = %s""",
                    (schema,),
                ).fetchall()
            }
            self.assertTrue(
                {
                    "paper_worker_heartbeats",
                    "paper_incident_events",
                    "paper_webhook_outbox",
                    "paper_reprocess_requests",
                    "paper_reprocess_targets",
                    "paper_reprocess_review_events",
                    "paper_reprocess_execution_jobs",
                    "paper_rollback_requests",
                    "paper_rollback_targets",
                    "paper_rollback_review_events",
                    "paper_rollback_execution_jobs",
                }
                <= tables
            )
            triggers = {
                row[0]
                for row in database.execute(
                    """SELECT trigger_name FROM information_schema.triggers
                       WHERE trigger_schema = %s""",
                    (schema,),
                ).fetchall()
            }
            self.assertTrue(
                {
                    "paper_reprocess_review_event_apply",
                    "paper_rollback_review_event_apply",
                    "paper_incident_events_immutable",
                    "paper_reprocess_requests_immutable",
                    "paper_reprocess_targets_immutable",
                    "paper_reprocess_review_events_immutable",
                    "paper_rollback_requests_immutable",
                    "paper_rollback_targets_immutable",
                    "paper_rollback_review_events_immutable",
                }
                <= triggers
            )
        finally:
            database.rollback()
            database.close()


if __name__ == "__main__":
    unittest.main()
