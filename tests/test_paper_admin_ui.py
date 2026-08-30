import json
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from paper_service import admin_queries
from paper_service.admin_views import render_admin_page


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class RecordingDatabase:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, sql, parameters=None):
        self.calls.append((sql, parameters))
        return Result(self.rows)


class AdminPaperQueryTests(unittest.TestCase):
    def test_choice_query_is_bounded_and_projection_is_allowlisted(self):
        database = RecordingDatabase(
            [
                {
                    "id": 9,
                    "title": "Neural decoding",
                    "category": "Neuro AI",
                    "publication_year": 2026,
                    "reader_v2_status": "ready",
                    "stored_path": "/private/paper.pdf",
                    "sha256": "sensitive-digest",
                    "annotation_body": "private-note-marker",
                    "lease_token": "private-lease-marker",
                }
            ]
        )
        with patch.object(
            admin_queries, "connection", return_value=nullcontext(database)
        ):
            choices = admin_queries.list_admin_paper_choices(limit=25)

        self.assertEqual(
            choices,
            (
                {
                    "paper_id": 9,
                    "title": "Neural decoding",
                    "category": "Neuro AI",
                    "publication_year": 2026,
                    "reader_status": "ready",
                },
            ),
        )
        serialized = json.dumps(choices, ensure_ascii=False)
        for forbidden in (
            "/private/paper.pdf",
            "sensitive-digest",
            "private-note-marker",
            "private-lease-marker",
        ):
            self.assertNotIn(forbidden, serialized)

        sql, parameters = database.calls[0]
        normalized = " ".join(sql.casefold().split())
        self.assertEqual(parameters, (25,))
        self.assertIn("limit %s", normalized)
        for forbidden_column in (
            "stored_path",
            "sha256",
            "annotation",
            "lease_token",
        ):
            self.assertNotIn(forbidden_column, normalized)

    def test_choice_limit_fails_before_database_access(self):
        for invalid in (0, 201, True, "10"):
            with self.subTest(invalid=invalid), patch.object(
                admin_queries, "connection"
            ) as connect:
                with self.assertRaises(admin_queries.AdminQueryError):
                    admin_queries.list_admin_paper_choices(limit=invalid)
                connect.assert_not_called()

    def test_invalid_display_metadata_fails_closed(self):
        database = RecordingDatabase(
            [
                {
                    "id": 9,
                    "title": "Bad\x00title",
                    "category": "Neuro AI",
                    "publication_year": 2026,
                    "reader_v2_status": "ready",
                }
            ]
        )
        with patch.object(
            admin_queries, "connection", return_value=nullcontext(database)
        ):
            with self.assertRaisesRegex(
                admin_queries.AdminQueryError, "admin_paper_title_invalid"
            ):
                admin_queries.list_admin_paper_choices()

    def test_source_binding_returns_only_id_and_verified_digest(self):
        database = RecordingDatabase(
            [
                {
                    "id": 9,
                    "sha256": "a" * 64,
                    "stored_path": "/private/paper.pdf",
                    "title": "private title marker",
                }
            ]
        )
        with patch.object(
            admin_queries, "connection", return_value=nullcontext(database)
        ):
            binding = admin_queries.get_admin_paper_source_binding(9)

        self.assertEqual(
            binding, {"paper_id": 9, "source_sha256": "a" * 64}
        )
        sql, parameters = database.calls[0]
        self.assertEqual(parameters, (9,))
        normalized = " ".join(sql.casefold().split())
        self.assertNotIn("stored_path", normalized)
        self.assertNotIn("title", normalized)

    def test_source_binding_missing_is_none_and_invalid_id_is_rejected(self):
        database = RecordingDatabase([])
        with patch.object(
            admin_queries, "connection", return_value=nullcontext(database)
        ):
            self.assertIsNone(admin_queries.get_admin_paper_source_binding(9))
        with patch.object(admin_queries, "connection") as connect:
            with self.assertRaises(admin_queries.AdminQueryError):
                admin_queries.get_admin_paper_source_binding(0)
            connect.assert_not_called()


class AdminMobileShellTests(unittest.TestCase):
    def test_page_is_mobile_first_external_script_and_human_readable(self):
        html = render_admin_page()

        self.assertIn('name="viewport"', html)
        self.assertIn('src="/static/admin.js" defer', html)
        self.assertEqual(html.count("<script"), 1)
        self.assertNotIn("<script>", html)
        self.assertIn("내 확인이 필요해요", html)
        self.assertIn("승인 전에는 재처리나 되돌리기가 실행되지 않습니다", html)
        self.assertIn("비용 안내", html)
        self.assertIn("aria-live", html)
        self.assertIn("prefers-reduced-motion", html)

    def test_controller_uses_safe_dom_and_explicit_confirmation_boundaries(self):
        script = (
            Path(__file__).parents[1] / "paper_service" / "static" / "admin.js"
        ).read_text(encoding="utf-8")

        self.assertIn('const ACTION_HEADER = "X-Paper-Action"', script)
        self.assertIn("window.confirm", script)
        self.assertIn("DeepSeek Pro", script)
        self.assertIn("제한된 제목 기반 주제어만", script)
        self.assertIn("파일명·폴더 경로·SHA는 전송하지 않", script)
        self.assertIn("confirm_selected_reprocess: true", script)
        self.assertIn("confirm_selected_rollback: true", script)
        self.assertIn("confirm_execution: true", script)
        self.assertIn('paper.reader_status === "ready"', script)
        self.assertIn("전체 재처리 요청은 이 화면에서 실행할 수 없습니다", script)
        self.assertNotIn("innerHTML", script)
        for endpoint in (
            "/api/admin/overview",
            "/api/admin/operations/reprocess",
            "/api/admin/operations/rollback",
            "/api/admin/taxonomy/audits/",
            "/api/admin/taxonomy/proposals/",
        ):
            self.assertIn(endpoint, script)


if __name__ == "__main__":
    unittest.main()
