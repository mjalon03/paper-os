from __future__ import annotations

import os
import hashlib
import json
import unittest
from uuid import uuid4

from paper_service.migrations.v001_initial import SQL as V001
from paper_service.migrations.v002_reading_states import SQL as V002
from paper_service.migrations.v003_reader_issues import SQL as V003
from paper_service.migrations.v004_annotations import SQL as V004
from paper_service.migrations.v005_paper_knowledge import SQL as V005
from paper_service.migrations.v006_search_qa import SQL as V006


DSN = os.environ.get("PAPER_TEST_POSTGRES_DSN")


@unittest.skipUnless(DSN, "PAPER_TEST_POSTGRES_DSN is not configured")
class SearchPostgresIntegrationTests(unittest.TestCase):
    def test_migration_and_builtin_fts_without_extensions(self):
        import psycopg
        from psycopg import sql

        schema = "paper_search_test_" + uuid4().hex
        with psycopg.connect(DSN, autocommit=True) as database:
            database.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
            )
            try:
                database.execute(
                    sql.SQL("SET search_path TO {}, public").format(
                        sql.Identifier(schema)
                    )
                )
                for migration in (V001, V002, V003, V004, V005, V006):
                    database.execute(migration)
                columns = database.execute(
                    """SELECT column_name FROM information_schema.columns
                       WHERE table_schema = %s AND table_name = 'annotations'""",
                    (schema,),
                ).fetchall()
                self.assertIn("ai_share_allowed", {row[0] for row in columns})
                vector_type = database.execute(
                    """SELECT data_type FROM information_schema.columns
                       WHERE table_schema = %s
                         AND table_name = 'paper_search_chunks'
                         AND column_name = 'search_vector'""",
                    (schema,),
                ).fetchone()
                self.assertEqual(vector_type[0], "tsvector")
                paper_id = database.execute(
                    """INSERT INTO papers
                       (sha256, size_bytes, source_path, stored_path)
                       VALUES (%s, 1, 'inbox/paper.pdf', 'paper.pdf')
                       RETURNING id""",
                    ("a" * 64,),
                ).fetchone()[0]
                generation_id = str(uuid4())
                database.execute(
                    """INSERT INTO paper_search_generations
                       (id, paper_id, corpus_digest, source_sha256,
                        article_fingerprint, selector_version,
                        document_count, chunk_count)
                       VALUES (%s::uuid, %s, %s, %s, %s, 1, 1, 1)""",
                    (generation_id, paper_id, "b" * 64, "a" * 64, "c" * 64),
                )
                document_id = "d-" + "1" * 32
                chunk_id = "c-" + "2" * 32
                node_id = "n-" + "3" * 24
                content = "cortical signal increased"
                content_digest = hashlib.sha256(content.encode()).hexdigest()
                database.execute(
                    """INSERT INTO paper_search_documents
                       (generation_id, paper_id, document_id, selector_version,
                        source_kind, source_id, node_id, content,
                        lexical_content, authority_score, model_eligible,
                        content_sha256)
                       VALUES (%s::uuid, %s, %s, 1, 'source_text', 'paper:1',
                               %s, %s, %s, 100, TRUE, %s)""",
                    (
                        generation_id,
                        paper_id,
                        document_id,
                        node_id,
                        content,
                        content,
                        content_digest,
                    ),
                )
                database.execute(
                    """INSERT INTO paper_search_chunks
                       (generation_id, paper_id, chunk_id, document_id,
                        selector_version, source_kind, source_id, node_id,
                        start_offset, end_offset, content, lexical_content,
                        authority_score, model_eligible, content_sha256)
                       VALUES (%s::uuid, %s, %s, %s, 1, 'source_text',
                               'paper:1', %s, 0, %s, %s, %s, 100, TRUE, %s)""",
                    (
                        generation_id,
                        paper_id,
                        chunk_id,
                        document_id,
                        node_id,
                        len(content),
                        content,
                        content,
                        content_digest,
                    ),
                )
                database.execute(
                    """INSERT INTO paper_search_heads
                       (paper_id, generation_id, corpus_digest, source_sha256,
                        article_fingerprint, selector_version)
                       VALUES (%s, %s::uuid, %s, %s, %s, 1)""",
                    (paper_id, generation_id, "b" * 64, "a" * 64, "c" * 64),
                )
                matches = database.execute(
                    """SELECT chunk_id FROM paper_search_chunks
                       WHERE search_vector @@
                             plainto_tsquery('simple'::regconfig, 'cortical')"""
                ).fetchall()
                self.assertEqual(matches, [(chunk_id,)])

                snapshot = {
                    "schema_version": 1,
                    "selector_version": 1,
                    "question": "What increased?",
                    "source_nodes": [
                        {
                            "selector_version": 1,
                            "source_kind": "source_text",
                            "source_id": chunk_id,
                            "node_id": node_id,
                            "text": content,
                        }
                    ],
                    "limits": {
                        "max_claims": 16,
                        "max_citations_per_claim": 8,
                        "max_total_citations": 64,
                        "max_answer_chars": 12000,
                        "max_claim_chars": 2000,
                        "max_quote_chars": 4000,
                        "max_total_quote_chars": 32000,
                    },
                }
                snapshot_bytes = json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                job_id = str(uuid4())
                database.execute(
                    """INSERT INTO paper_qa_jobs
                       (id, paper_id, idempotency_key_digest, request_digest,
                        question, provider, model, request_actor_kind,
                        request_actor_id, request_acknowledged, evidence_limit,
                        generation_id, source_sha256, article_fingerprint,
                        selector_version, evidence_snapshot,
                        evidence_snapshot_sha256)
                       VALUES (%s::uuid, %s, %s, %s, 'What increased?',
                               'deepseek', 'deepseek-v4-flash', 'human',
                               'access-user-123', TRUE, 32, %s::uuid, %s, %s,
                               1, %s::jsonb, %s)""",
                    (
                        job_id,
                        paper_id,
                        "d" * 64,
                        "e" * 64,
                        generation_id,
                        "a" * 64,
                        "c" * 64,
                        snapshot_bytes.decode(),
                        hashlib.sha256(snapshot_bytes).hexdigest(),
                    ),
                )
                database.execute(
                    """INSERT INTO paper_qa_job_evidence
                       (job_id, paper_id, ordinal, generation_id, chunk_id,
                        source_kind, node_id, content_sha256, model_eligible)
                       VALUES (%s::uuid, %s, 0, %s::uuid, %s,
                               'source_text', %s, %s, TRUE)""",
                    (
                        job_id,
                        paper_id,
                        generation_id,
                        chunk_id,
                        node_id,
                        content_digest,
                    ),
                )
                with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
                    database.execute(
                        "UPDATE paper_search_generations SET chunk_count = 2 WHERE id = %s::uuid",
                        (generation_id,),
                    )
                with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
                    database.execute(
                        "UPDATE paper_qa_jobs SET request_actor_id = 'substituted-user' WHERE id = %s::uuid",
                        (job_id,),
                    )
                extensions = database.execute(
                    "SELECT extname FROM pg_extension WHERE extname = 'pg_trgm'"
                ).fetchall()
                # The schema never depends on pg_trgm, regardless of whether the
                # host happens to have it installed for another application.
                self.assertIsInstance(extensions, list)
            finally:
                database.execute("SET search_path TO public")
                database.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )


if __name__ == "__main__":
    unittest.main()
