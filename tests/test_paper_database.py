import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from paper_service.database import (
    MIGRATION_ADVISORY_LOCK_ID,
    MigrationError,
    PaperNotFound,
    ReadingStateConflict,
    _update_reading_state,
    apply_migrations,
    ensure_schema,
    update_reading_state,
)
from paper_service.migrations import MIGRATIONS
from paper_service.migrations.v001_initial import SQL as INITIAL_SCHEMA_SQL
from paper_service.migrations.v002_reading_states import SQL as READING_STATES_SQL


class Result:
    def __init__(self, *, one=None, many=None):
        self.one = one
        self.many = [] if many is None else many

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class MigrationDatabase:
    def __init__(self, applied=None):
        self.applied = {} if applied is None else dict(applied)
        self.calls = []

    def execute(self, sql, parameters=None):
        self.calls.append((sql, parameters))
        if sql.startswith("SELECT version, name, checksum"):
            rows = [self.applied[version] for version in sorted(self.applied)]
            return Result(many=rows)
        if sql.startswith("INSERT INTO schema_migrations"):
            version, name, checksum = parameters
            self.applied[version] = {
                "version": version,
                "name": name,
                "checksum": checksum,
            }
        return Result()


class SequenceDatabase:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def execute(self, sql, parameters=None):
        self.calls.append((sql, parameters))
        return next(self.results)


def reading_state(**overrides):
    state = {
        "paper_id": 12,
        "status": "reading",
        "progress": 0.25,
        "anchor": {
            "selector_version": 1,
            "scope": "body",
            "section_id": "s-" + "1" * 24,
            "node_id": "n-" + "2" * 24,
            "article_fingerprint": "a" * 64,
        },
        "article_fingerprint": "a" * 64,
        "favorite": False,
        "priority": 0,
        "created_at": datetime(2026, 8, 29, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 8, 29, tzinfo=timezone.utc),
        "completed_at": None,
        "revision": 2,
    }
    state.update(overrides)
    return state


class MigrationTests(unittest.TestCase):
    def test_startup_schema_initialization_runs_migration_framework(self):
        database = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = database
        with (
            patch("paper_service.database.connection", return_value=context),
            patch("paper_service.database.apply_migrations") as migrate,
        ):
            ensure_schema()

        migrate.assert_called_once_with(database)

    def test_applies_pending_migrations_in_order_and_records_checksums(self):
        database = MigrationDatabase()

        apply_migrations(database)

        self.assertEqual(
            database.calls[0],
            ("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_ADVISORY_LOCK_ID,)),
        )
        migration_sql_calls = [
            sql
            for sql, _parameters in database.calls
            if sql in {migration.sql for migration in MIGRATIONS}
        ]
        self.assertEqual(migration_sql_calls, [migration.sql for migration in MIGRATIONS])
        self.assertEqual(
            [database.applied[version]["checksum"] for version in sorted(database.applied)],
            [migration.checksum for migration in MIGRATIONS],
        )

    def test_repeated_run_does_not_reapply_recorded_migrations(self):
        database = MigrationDatabase(
            {
                migration.version: {
                    "version": migration.version,
                    "name": migration.name,
                    "checksum": migration.checksum,
                }
                for migration in MIGRATIONS
            }
        )

        apply_migrations(database)

        self.assertFalse(
            any(
                sql in {migration.sql for migration in MIGRATIONS}
                for sql, _parameters in database.calls
            )
        )

    def test_changed_migration_history_fails_closed(self):
        migration = MIGRATIONS[0]
        database = MigrationDatabase(
            {
                migration.version: {
                    "version": migration.version,
                    "name": migration.name,
                    "checksum": "0" * 64,
                }
            }
        )

        with self.assertRaisesRegex(MigrationError, "history does not match"):
            apply_migrations(database)

    def test_unknown_newer_migration_fails_closed(self):
        database = MigrationDatabase(
            {99: {"version": 99, "name": "future", "checksum": "0" * 64}}
        )

        with self.assertRaisesRegex(MigrationError, "unknown migration versions: 99"):
            apply_migrations(database)

    def test_migration_history_gap_fails_closed(self):
        migration = MIGRATIONS[1]
        database = MigrationDatabase(
            {
                migration.version: {
                    "version": migration.version,
                    "name": migration.name,
                    "checksum": migration.checksum,
                }
            }
        )

        with self.assertRaisesRegex(MigrationError, "not an ordered prefix"):
            apply_migrations(database)

    def test_reading_state_migration_enforces_core_invariants(self):
        self.assertIn(
            "PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE",
            READING_STATES_SQL,
        )
        for status in ("unread", "reading", "completed", "on_hold", "rereading"):
            self.assertIn(f"'{status}'", READING_STATES_SQL)
        self.assertIn("progress >= 0 AND progress <= 1", READING_STATES_SQL)
        self.assertIn("jsonb_typeof(anchor) = 'object'", READING_STATES_SQL)
        self.assertIn("revision BIGINT NOT NULL DEFAULT 1", READING_STATES_SQL)

    def test_initial_migration_validates_legacy_baseline_guards(self):
        guard_names = (
            "sync_jobs_trigger_v001_guard",
            "sync_jobs_status_v001_guard",
            "sync_jobs_files_seen_v001_guard",
            "sync_jobs_files_imported_v001_guard",
            "sync_jobs_files_duplicate_v001_guard",
            "sync_jobs_files_invalid_v001_guard",
            "sync_jobs_files_organized_v001_guard",
            "sync_jobs_files_processing_failed_v001_guard",
            "sync_jobs_files_reader_ready_v001_guard",
            "sync_jobs_files_reader_failed_v001_guard",
            "papers_sha256_v001_guard",
            "papers_size_bytes_v001_guard",
            "papers_processing_status_v001_guard",
            "papers_processing_attempts_v001_guard",
            "papers_reader_status_v001_guard",
            "papers_reader_attempts_v001_guard",
            "papers_reader_v2_status_v001_guard",
            "papers_reader_v2_attempts_v001_guard",
        )
        for guard in guard_names:
            with self.subTest(guard=guard):
                self.assertIn(f"CONSTRAINT {guard}", INITIAL_SCHEMA_SQL)
                self.assertIn(f"VALIDATE CONSTRAINT {guard}", INITIAL_SCHEMA_SQL)
        self.assertIn("CHECK (btrim(sha256) ~ '^[0-9a-f]{64}$')", INITIAL_SCHEMA_SQL)
        self.assertIn("CHECK (processing_attempts >= 0)", INITIAL_SCHEMA_SQL)
        self.assertIn("CHECK (reader_v2_attempts >= 0)", INITIAL_SCHEMA_SQL)


class ReadingStateTests(unittest.TestCase):
    def test_first_write_uses_logical_revision_zero(self):
        inserted = reading_state(revision=1, progress=0.4, favorite=True)
        database = SequenceDatabase(
            [Result(one=None), Result(one={"id": 12}), Result(one=inserted)]
        )

        result = _update_reading_state(
            database,
            paper_id=12,
            expected_revision=0,
            values={"progress": 0.4, "favorite": True},
        )

        self.assertEqual(result, inserted)
        insert_sql, parameters = database.calls[2]
        self.assertIn("ON CONFLICT (paper_id) DO NOTHING", insert_sql)
        self.assertEqual(parameters[0:3], (12, "unread", 0.4))
        self.assertEqual(parameters[5:7], (True, 0))

    def test_patch_merges_fields_and_increments_revision(self):
        current = reading_state()
        updated = reading_state(
            status="completed", progress=1.0, favorite=True, priority=4, revision=3
        )
        database = SequenceDatabase([Result(one=current), Result(one=updated)])

        result = _update_reading_state(
            database,
            paper_id=12,
            expected_revision=2,
            values={
                "status": "completed",
                "progress": 1.0,
                "favorite": True,
                "priority": 4,
            },
        )

        self.assertEqual(result, updated)
        update_sql, parameters = database.calls[1]
        self.assertIn("revision = revision + 1", update_sql)
        self.assertIn("COALESCE(completed_at, NOW())", update_sql)
        self.assertEqual(parameters[-2:], (12, 2))
        self.assertEqual(
            parameters[2],
            '{"selector_version":1,"scope":"body",'
            '"section_id":"s-111111111111111111111111",'
            '"node_id":"n-222222222222222222222222",'
            '"article_fingerprint":"' + "a" * 64 + '"}',
        )

    def test_stale_revision_returns_current_state_without_writing(self):
        current = reading_state(revision=7)
        database = SequenceDatabase([Result(one=current)])

        with self.assertRaises(ReadingStateConflict) as raised:
            _update_reading_state(
                database,
                paper_id=12,
                expected_revision=6,
                values={"progress": 0.5},
            )

        self.assertEqual(raised.exception.current_state, current)
        self.assertEqual(len(database.calls), 1)

    def test_missing_paper_is_distinct_from_revision_conflict(self):
        database = SequenceDatabase([Result(one=None), Result(one=None)])

        with self.assertRaises(PaperNotFound):
            _update_reading_state(
                database,
                paper_id=987,
                expected_revision=0,
                values={"progress": 0.5},
            )

    def test_write_validation_rejects_invalid_values_before_connecting(self):
        invalid_calls = (
            {"paper_id": 0, "expected_revision": 0, "progress": 0.5},
            {"paper_id": 1, "expected_revision": -1, "progress": 0.5},
            {"paper_id": 1, "expected_revision": 0, "progress": float("nan")},
            {"paper_id": 1, "expected_revision": 0, "anchor": []},
            {"paper_id": 1, "expected_revision": 0, "favorite": 1},
            {"paper_id": 1, "expected_revision": 0, "priority": 6},
            {"paper_id": 1, "expected_revision": 0, "status": "finished"},
            {"paper_id": 1, "expected_revision": 0},
        )
        for arguments in invalid_calls:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    update_reading_state(**arguments)


if __name__ == "__main__":
    unittest.main()
