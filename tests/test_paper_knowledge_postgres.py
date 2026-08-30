"""Opt-in PostgreSQL 17 migration probe.

Set ``PAPER_KNOWLEDGE_TEST_DSN`` to an isolated PostgreSQL database.  The probe
creates a random schema and rolls the entire transaction back; it never prints
the DSN or credentials.
"""

import os
import unittest
from uuid import uuid4

from paper_service.migrations import MIGRATIONS


class PaperKnowledgePostgresProbe(unittest.TestCase):
    def test_all_migrations_apply_in_isolated_rollback_schema(self):
        dsn = os.environ.get("PAPER_KNOWLEDGE_TEST_DSN")
        if not dsn:
            self.skipTest("PAPER_KNOWLEDGE_TEST_DSN is not configured")
        try:
            import psycopg
        except ImportError:
            self.skipTest("psycopg is not installed")

        schema = "paper_knowledge_probe_" + uuid4().hex
        self.assertRegex(schema, r"^[a-z0-9_]+$")
        database = psycopg.connect(dsn)
        try:
            database.execute(f'CREATE SCHEMA "{schema}"')
            database.execute(f'SET LOCAL search_path TO "{schema}"')
            for migration in MIGRATIONS:
                database.execute(migration.sql)

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
                    "paper_knowledge_jobs",
                    "paper_knowledge_revisions",
                    "paper_knowledge_review_events",
                    "paper_knowledge_outbox",
                    "paper_knowledge_usage_ledger",
                }
                <= tables
            )
            triggers = {
                row[0]
                for row in database.execute(
                    """SELECT trigger_name
                       FROM information_schema.triggers
                       WHERE trigger_schema = %s""",
                    (schema,),
                ).fetchall()
            }
            self.assertTrue(
                {
                    "paper_knowledge_jobs_identity_immutable",
                    "paper_knowledge_review_event_validate",
                    "paper_knowledge_revisions_immutable",
                    "paper_knowledge_review_events_immutable",
                    "paper_knowledge_usage_ledger_immutable",
                }
                <= triggers,
            )
            paper_id = database.execute(
                """INSERT INTO papers
                   (sha256, size_bytes, source_path, stored_path)
                   VALUES (%s, 1, 'inbox/paper.pdf', 'paper.pdf')
                   RETURNING id""",
                ("a" * 64,),
            ).fetchone()[0]
            job_id = str(uuid4())
            database.execute(
                """INSERT INTO paper_knowledge_jobs
                   (id, paper_id, idempotency_key_digest, request_digest,
                    provider, model, request_actor_kind, request_actor_id,
                    paper_text_and_cost_acknowledged,
                    include_personal_annotations, max_source_pages,
                    max_source_characters, max_output_tokens, max_attempts)
                   VALUES (%s::uuid, %s, %s, %s, 'deepseek',
                           'deepseek-v4-pro', 'human', 'access-user-123', TRUE,
                           FALSE, 8, 30000, 4096, 3)""",
                (job_id, paper_id, "b" * 64, "c" * 64),
            )
            with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
                database.execute(
                    """UPDATE paper_knowledge_jobs
                       SET request_actor_id = 'substituted-user'
                       WHERE id = %s::uuid""",
                    (job_id,),
                )
        finally:
            database.rollback()
            database.close()


if __name__ == "__main__":
    unittest.main()
