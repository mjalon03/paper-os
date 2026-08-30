import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID

from paper_service.database import (
    AnnotationConflict,
    AnnotationTrashed,
    ReaderIssueConflict,
    _insert_reader_issue,
    _system_warning_values,
    _update_annotation,
    _update_reader_issue,
    create_annotation,
    create_reader_issue,
    import_article_warnings,
    list_annotations,
    list_reader_issues,
    trash_annotation,
    update_annotation,
    update_reading_state,
)
from paper_service.migrations import MIGRATIONS
from paper_service.migrations.v003_reader_issues import SQL as READER_ISSUES_SQL
from paper_service.migrations.v004_annotations import SQL as ANNOTATIONS_SQL


FINGERPRINT = "a" * 64
ANNOTATION_ID = "85b11c43-bad7-44ba-9bb4-07a5601fd978"
ISSUE_ID = "16d7ea28-a033-416c-82ac-5c6f90d684f9"
NOW = datetime(2026, 8, 29, tzinfo=timezone.utc)


class Result:
    def __init__(self, *, one=None, many=None):
        self.one = one
        self.many = [] if many is None else many

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class SequenceDatabase:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def execute(self, sql, parameters=None):
        self.calls.append((sql, parameters))
        return next(self.results)


def database_context(database):
    context = MagicMock()
    context.__enter__.return_value = database
    context.__exit__.return_value = False
    return context


def selection_anchor(**overrides):
    # "🧠AI" occupies four UTF-16 code units: two for the emoji and two ASCII.
    value = {
        "selector_version": 1,
        "scope": "body",
        "section_id": "s-" + "1" * 24,
        "node_id": "n-" + "2" * 24,
        "start_utf16": 3,
        "end_utf16": 7,
        "exact_quote": "🧠AI",
        "prefix": "before ",
        "suffix": " after",
        "article_fingerprint": FINGERPRINT,
        "page": 2,
    }
    value.update(overrides)
    return value


def article_anchor(**overrides):
    value = {
        "selector_version": 1,
        "scope": "article",
        "article_fingerprint": FINGERPRINT,
    }
    value.update(overrides)
    return value


def issue_row(**overrides):
    value = {
        "id": UUID(ISSUE_ID),
        "paper_id": 12,
        "kind": "sentence_break",
        "description": "A sentence is split.",
        "anchor": selection_anchor(),
        "article_fingerprint": FINGERPRINT,
        "source": "user",
        "external_key": None,
        "status": "open",
        "created_at": NOW,
        "updated_at": NOW,
        "resolved_at": None,
        "revision": 1,
    }
    value.update(overrides)
    return value


def annotation_row(**overrides):
    value = {
        "id": UUID(ANNOTATION_ID),
        "paper_id": 12,
        "kind": "highlight",
        "anchor": selection_anchor(),
        "body": None,
        "category": "methods",
        "question_status": None,
        "article_fingerprint": FINGERPRINT,
        "ai_share_allowed": False,
        "created_at": NOW,
        "updated_at": NOW,
        "trashed_at": None,
        "revision": 1,
    }
    value.update(overrides)
    return value


def reading_state_row(**overrides):
    value = {
        "paper_id": 12,
        "status": "reading",
        "progress": 0.4,
        "anchor": {
            "selector_version": 1,
            "scope": "body",
            "section_id": "s-" + "1" * 24,
            "node_id": "n-" + "2" * 24,
            "article_fingerprint": FINGERPRINT,
        },
        "article_fingerprint": FINGERPRINT,
        "favorite": False,
        "priority": 0,
        "created_at": NOW,
        "updated_at": NOW,
        "completed_at": None,
        "revision": 1,
    }
    value.update(overrides)
    return value


class InteractionMigrationTests(unittest.TestCase):
    def test_issue_and_annotation_migrations_follow_reading_state(self):
        self.assertEqual(
            [
                (migration.version, migration.name)
                for migration in MIGRATIONS
                if 2 <= migration.version <= 4
            ],
            [
                (2, "reading_states"),
                (3, "reader_issues"),
                (4, "annotations"),
            ],
        )

    def test_issue_schema_preserves_soft_resolution_and_idempotent_warnings(self):
        self.assertIn("id UUID PRIMARY KEY", READER_ISSUES_SQL)
        self.assertIn("status IN ('open', 'resolved', 'dismissed')", READER_ISSUES_SQL)
        self.assertIn("revision BIGINT NOT NULL DEFAULT 1", READER_ISSUES_SQL)
        self.assertIn("ON reader_issues (paper_id, external_key)", READER_ISSUES_SQL)
        self.assertIn("WHERE external_key IS NOT NULL", READER_ISSUES_SQL)
        self.assertNotIn("deleted_at", READER_ISSUES_SQL)

    def test_annotation_schema_uses_soft_trash_and_selection_evidence(self):
        self.assertIn("kind IN ('highlight', 'note', 'question')", ANNOTATIONS_SQL)
        self.assertIn("'start_utf16', 'end_utf16', 'exact_quote'", ANNOTATIONS_SQL)
        self.assertIn("trashed_at TIMESTAMPTZ", ANNOTATIONS_SQL)
        self.assertIn("revision BIGINT NOT NULL DEFAULT 1", ANNOTATIONS_SQL)
        self.assertNotIn("ON DELETE SET NULL", ANNOTATIONS_SQL)


class ReaderIssueTests(unittest.TestCase):
    def test_client_identified_user_issue_is_created_at_revision_one(self):
        created = issue_row()
        database = SequenceDatabase([Result(one={"id": 12}), Result(one=created)])
        with patch(
            "paper_service.database.connection",
            return_value=database_context(database),
        ):
            result = create_reader_issue(
                12,
                issue_id=ISSUE_ID,
                kind="sentence_break",
                description="A sentence is split.",
                anchor=selection_anchor(),
                article_fingerprint=FINGERPRINT,
            )

        self.assertEqual(result, created)
        insert_sql, parameters = database.calls[1]
        self.assertIn("ON CONFLICT DO NOTHING", insert_sql)
        self.assertEqual(parameters[0:4], (ISSUE_ID, 12, "sentence_break", "A sentence is split."))
        self.assertEqual(json.loads(parameters[4]), selection_anchor())

    def test_resolving_issue_increments_revision_and_retains_record(self):
        current = issue_row(revision=3)
        resolved = issue_row(status="resolved", resolved_at=NOW, revision=4)
        database = SequenceDatabase([Result(one=current), Result(one=resolved)])

        result = _update_reader_issue(
            database,
            issue_id=ISSUE_ID,
            expected_revision=3,
            values={"status": "resolved"},
        )

        self.assertEqual(result, resolved)
        update_sql, parameters = database.calls[1]
        self.assertIn("COALESCE(resolved_at, NOW())", update_sql)
        self.assertIn("revision = revision + 1", update_sql)
        self.assertNotIn("DELETE", update_sql)
        self.assertEqual(parameters[-2:], (ISSUE_ID, 3))

    def test_stale_issue_revision_returns_current_issue_without_write(self):
        current = issue_row(revision=5)
        database = SequenceDatabase([Result(one=current)])

        with self.assertRaises(ReaderIssueConflict) as raised:
            _update_reader_issue(
                database,
                issue_id=ISSUE_ID,
                expected_revision=4,
                values={"status": "dismissed"},
            )

        self.assertEqual(raised.exception.current_issue, current)
        self.assertEqual(len(database.calls), 1)

    def test_warning_import_is_deterministic_idempotent_and_never_reopens(self):
        values = _system_warning_values(
            12, "figure_crop_missing:page-7", FINGERPRINT
        )
        existing = issue_row(
            id=UUID(values["id"]),
            kind="figure_crop",
            description="figure_crop_missing:page-7",
            anchor=values["anchor"],
            source="system",
            external_key=values["external_key"],
            status="dismissed",
            resolved_at=NOW,
            revision=4,
        )
        database = SequenceDatabase(
            [Result(one={"id": 12}), Result(one=None), Result(one=existing)]
        )
        with patch(
            "paper_service.database.connection",
            return_value=database_context(database),
        ):
            imported = import_article_warnings(
                12,
                warnings=["figure_crop_missing:page-7", "figure_crop_missing:page-7"],
                article_fingerprint=FINGERPRINT,
            )

        self.assertEqual(imported, [existing])
        self.assertEqual(values["anchor"]["page"], 7)
        self.assertEqual(values["id"], _system_warning_values(
            12, "figure_crop_missing:page-7", FINGERPRINT
        )["id"])
        self.assertEqual(existing["status"], "dismissed")

    def test_warning_reimport_after_rebuild_keeps_reviewed_anchor(self):
        values = _system_warning_values(12, "section_title_missing:3", "b" * 64)
        existing = issue_row(
            id=UUID(values["id"]),
            kind="section_structure",
            description="section_title_missing:3",
            anchor=article_anchor(),
            source="system",
            external_key=values["external_key"],
            status="resolved",
            resolved_at=NOW,
            revision=2,
        )
        database = SequenceDatabase([Result(one=None), Result(one=existing)])

        result = _insert_reader_issue(database, values)

        self.assertEqual(result, existing)
        self.assertEqual(result["article_fingerprint"], FINGERPRINT)
        self.assertEqual(result["revision"], 2)

    def test_issue_filters_are_validated_and_parameterized(self):
        database = SequenceDatabase([Result(many=[issue_row()])])
        with patch(
            "paper_service.database.connection",
            return_value=database_context(database),
        ):
            result = list_reader_issues(12, status="open", source="user")

        self.assertEqual(result, [issue_row()])
        sql, parameters = database.calls[0]
        self.assertIn("status = %s", sql)
        self.assertIn("source = %s", sql)
        self.assertEqual(parameters, (12, "open", "user"))

    def test_issue_validation_happens_before_any_sql(self):
        invalid_calls = (
            {"paper_id": 0, "kind": "other", "anchor": article_anchor()},
            {"paper_id": 12, "kind": "layout", "anchor": article_anchor()},
            {
                "paper_id": 12,
                "kind": "other",
                "anchor": article_anchor(),
                "source": "system",
            },
            {
                "paper_id": 12,
                "kind": "other",
                "anchor": article_anchor(article_fingerprint="b" * 64),
            },
            {
                "paper_id": 12,
                "kind": "other",
                "anchor": article_anchor(start_utf16=0),
            },
        )
        for arguments in invalid_calls:
            arguments.setdefault("article_fingerprint", FINGERPRINT)
            with self.subTest(arguments=arguments), patch(
                "paper_service.database.connection"
            ) as connect:
                with self.assertRaises(ValueError):
                    create_reader_issue(**arguments)
                connect.assert_not_called()


class AnnotationTests(unittest.TestCase):
    def test_question_defaults_to_open_and_exact_retry_is_idempotent(self):
        existing = annotation_row(
            kind="question", body="Why?", question_status="open"
        )
        database = SequenceDatabase(
            [Result(one={"id": 12}), Result(one=None), Result(one=existing)]
        )
        with patch(
            "paper_service.database.connection",
            return_value=database_context(database),
        ):
            result = create_annotation(
                ANNOTATION_ID,
                12,
                kind="question",
                anchor=selection_anchor(),
                body="Why?",
                category="methods",
                article_fingerprint=FINGERPRINT,
            )

        self.assertEqual(result, existing)
        insert_parameters = database.calls[1][1]
        self.assertEqual(insert_parameters[6], "open")
        self.assertIs(insert_parameters[8], False)

    def test_annotation_ai_sharing_requires_a_real_boolean(self):
        for invalid in (None, 0, 1, "true"):
            with self.subTest(invalid=invalid), patch(
                "paper_service.database.connection"
            ) as connect:
                with self.assertRaises(ValueError):
                    create_annotation(
                        ANNOTATION_ID,
                        12,
                        kind="note",
                        anchor=selection_anchor(),
                        body="private",
                        article_fingerprint=FINGERPRINT,
                        ai_share_allowed=invalid,
                    )
                connect.assert_not_called()

    def test_annotation_ai_sharing_can_be_changed_with_revision_locking(self):
        current = annotation_row(revision=2)
        updated = annotation_row(ai_share_allowed=True, revision=3)
        database = SequenceDatabase([Result(one=current), Result(one=updated)])

        result = _update_annotation(
            database,
            annotation_id=ANNOTATION_ID,
            expected_revision=2,
            values={"ai_share_allowed": True},
        )

        self.assertIs(result["ai_share_allowed"], True)
        update_parameters = database.calls[1][1]
        self.assertIs(update_parameters[5], True)

    def test_body_only_update_preserves_anchor_without_automatic_remap(self):
        current = annotation_row(revision=2)
        updated = annotation_row(body="My interpretation", revision=3)
        database = SequenceDatabase([Result(one=current), Result(one=updated)])

        result = _update_annotation(
            database,
            annotation_id=ANNOTATION_ID,
            expected_revision=2,
            values={"body": "My interpretation"},
        )

        self.assertEqual(result, updated)
        update_parameters = database.calls[1][1]
        self.assertEqual(json.loads(update_parameters[0]), selection_anchor())
        self.assertEqual(update_parameters[-2:], (ANNOTATION_ID, 2))

    def test_stale_annotation_update_is_rejected_before_write(self):
        current = annotation_row(revision=8)
        database = SequenceDatabase([Result(one=current)])

        with self.assertRaises(AnnotationConflict) as raised:
            _update_annotation(
                database,
                annotation_id=ANNOTATION_ID,
                expected_revision=7,
                values={"body": "stale"},
            )

        self.assertEqual(raised.exception.current_annotation, current)
        self.assertEqual(len(database.calls), 1)

    def test_trashed_annotation_cannot_be_edited(self):
        current = annotation_row(trashed_at=NOW, revision=3)
        database = SequenceDatabase([Result(one=current)])

        with self.assertRaises(AnnotationTrashed):
            _update_annotation(
                database,
                annotation_id=ANNOTATION_ID,
                expected_revision=3,
                values={"body": "must not write"},
            )

        self.assertEqual(len(database.calls), 1)

    def test_trash_is_soft_revisioned_and_idempotent_at_current_revision(self):
        current = annotation_row(revision=2)
        trashed = annotation_row(trashed_at=NOW, revision=3)
        database = SequenceDatabase([Result(one=current), Result(one=trashed)])
        with patch(
            "paper_service.database.connection",
            return_value=database_context(database),
        ):
            result = trash_annotation(ANNOTATION_ID, expected_revision=2)

        self.assertEqual(result, trashed)
        update_sql = database.calls[1][0]
        self.assertIn("trashed_at = NOW()", update_sql)
        self.assertIn("revision = revision + 1", update_sql)
        self.assertNotIn("DELETE", update_sql)

        already_trashed = SequenceDatabase([Result(one=trashed)])
        with patch(
            "paper_service.database.connection",
            return_value=database_context(already_trashed),
        ):
            retried = trash_annotation(ANNOTATION_ID, expected_revision=3)
        self.assertEqual(retried, trashed)
        self.assertEqual(len(already_trashed.calls), 1)

    def test_list_hides_trash_unless_explicitly_requested(self):
        active_database = SequenceDatabase([Result(many=[])])
        with patch(
            "paper_service.database.connection",
            return_value=database_context(active_database),
        ):
            list_annotations(12)
        self.assertIn("trashed_at IS NULL", active_database.calls[0][0])

        all_database = SequenceDatabase([Result(many=[])])
        with patch(
            "paper_service.database.connection",
            return_value=database_context(all_database),
        ):
            list_annotations(12, include_trashed=True)
        self.assertNotIn("trashed_at IS NULL", all_database.calls[0][0])

    def test_annotation_validation_happens_before_any_sql(self):
        invalid_calls = (
            {"annotation_id": "bad", "kind": "highlight", "anchor": selection_anchor()},
            {
                "annotation_id": ANNOTATION_ID,
                "kind": "comment",
                "anchor": selection_anchor(),
            },
            {
                "annotation_id": ANNOTATION_ID,
                "kind": "highlight",
                "anchor": selection_anchor(end_utf16=6),
            },
            {
                "annotation_id": ANNOTATION_ID,
                "kind": "highlight",
                "anchor": selection_anchor(),
                "question_status": "open",
            },
            {
                "annotation_id": ANNOTATION_ID,
                "kind": "highlight",
                "anchor": selection_anchor(),
                "expected_revision": 0.0,
            },
        )
        for arguments in invalid_calls:
            with self.subTest(arguments=arguments), patch(
                "paper_service.database.connection"
            ) as connect:
                with self.assertRaises(ValueError):
                    create_annotation(
                        paper_id=12,
                        article_fingerprint=FINGERPRINT,
                        **arguments,
                    )
                connect.assert_not_called()

    def test_anchor_and_fingerprint_can_only_be_replaced_together(self):
        with patch("paper_service.database.connection") as connect:
            with self.assertRaisesRegex(ValueError, "updated together"):
                update_annotation(
                    ANNOTATION_ID,
                    expected_revision=1,
                    anchor=selection_anchor(),
                )
            connect.assert_not_called()

    def test_duplicate_selector_suffix_ten_is_valid(self):
        anchor = selection_anchor(node_id="n-" + "2" * 24 + "-10")
        database = SequenceDatabase(
            [Result(one={"id": 12}), Result(one=annotation_row(anchor=anchor))]
        )
        with patch(
            "paper_service.database.connection",
            return_value=database_context(database),
        ):
            result = create_annotation(
                ANNOTATION_ID,
                12,
                kind="highlight",
                anchor=anchor,
                category="methods",
                article_fingerprint=FINGERPRINT,
            )
        self.assertEqual(result["anchor"]["node_id"], "n-" + "2" * 24 + "-10")


class ReadingAnchorValidationTests(unittest.TestCase):
    def test_first_non_null_resume_anchor_requires_and_stores_fingerprint(self):
        anchor = reading_state_row()["anchor"]
        inserted = reading_state_row()
        database = SequenceDatabase(
            [Result(one=None), Result(one={"id": 12}), Result(one=inserted)]
        )
        with patch(
            "paper_service.database.connection",
            return_value=database_context(database),
        ):
            result = update_reading_state(
                12,
                expected_revision=0,
                progress=0.4,
                anchor=anchor,
                article_fingerprint=FINGERPRINT,
            )

        self.assertEqual(result, inserted)
        self.assertEqual(json.loads(database.calls[2][1][3]), anchor)

    def test_mismatched_resume_anchor_is_rejected_before_sql(self):
        anchor = dict(reading_state_row()["anchor"])
        anchor["article_fingerprint"] = "b" * 64
        with patch("paper_service.database.connection") as connect:
            with self.assertRaisesRegex(ValueError, "does not match"):
                update_reading_state(
                    12,
                    expected_revision=0,
                    anchor=anchor,
                    article_fingerprint=FINGERPRINT,
                )
            connect.assert_not_called()

    def test_existing_anchor_must_match_a_fingerprint_only_update(self):
        current = reading_state_row(revision=3)
        database = SequenceDatabase([Result(one=current)])
        with patch(
            "paper_service.database.connection",
            return_value=database_context(database),
        ):
            with self.assertRaisesRegex(ValueError, "does not match"):
                update_reading_state(
                    12,
                    expected_revision=3,
                    article_fingerprint="b" * 64,
                )
        self.assertEqual(len(database.calls), 1)

    def test_existing_fingerprint_can_validate_an_anchor_only_update(self):
        current = reading_state_row(revision=2)
        anchor = dict(current["anchor"], node_id="n-" + "3" * 24 + "-10")
        updated = reading_state_row(anchor=anchor, revision=3)
        database = SequenceDatabase([Result(one=current), Result(one=updated)])
        with patch(
            "paper_service.database.connection",
            return_value=database_context(database),
        ):
            result = update_reading_state(
                12,
                expected_revision=2,
                anchor=anchor,
            )
        self.assertEqual(result, updated)


if __name__ == "__main__":
    unittest.main()
