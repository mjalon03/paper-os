import os
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from infra.papers.ops.paper_backup import (
    BACKUP_MARKER_CONTENT,
    BACKUP_MARKER_NAME,
    CONSTRAINT_QUERY,
    COUNT_QUERY,
    INTEGRITY_QUERY,
    PAPER_SHA_QUERY,
    POSTGRES_IMAGE,
    QUIESCE_SERVICES,
    TABLE_QUERY,
    CommandRunner,
    DatabaseSnapshot,
    OpsError,
    create_backup,
    initialize_backup_root,
    materialize_restore,
    parse_crypt_remote,
    pg_dump_command,
    remove_retention_candidates,
    restore_drill,
    retention_candidates,
    upload_backup,
    validate_rclone_config,
    validate_service_paths,
    verify_backup_directory,
    verify_crypt_remote,
    write_manifest,
    write_private_json,
)


NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
TABLES = ("papers", "schema_migrations", "sync_jobs")


def completed(command, returncode=0, stdout=b""):
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=b"")


class FakeRunner(CommandRunner):
    def __init__(
        self,
        *,
        restored_failure=False,
        remote_type="crypt",
        running_services=QUIESCE_SERVICES,
    ):
        self.calls = []
        self.restored_failure = restored_failure
        self.remote_type = remote_type
        self.running_services = tuple(running_services)
        self.quiesced = False

    def run(
        self,
        command,
        *,
        stage,
        stdin_path=None,
        stdout_path=None,
        capture=False,
        check=True,
    ):
        command = list(command)
        self.calls.append(
            {
                "command": command,
                "stage": stage,
                "stdin_path": stdin_path,
                "stdout_path": stdout_path,
                "capture": capture,
                "check": check,
            }
        )
        if stdout_path is not None:
            Path(stdout_path).write_bytes(b"valid-custom-format-placeholder")
        if capture and "--all" in command and "--quiet" in command and command[-1] == "paper-api":
            return completed(command, stdout=("a" * 64 + "\n").encode())
        if command[:3] == ["docker", "inspect", "--format={{.Image}}"]:
            return completed(command, stdout=("sha256:" + "b" * 64 + "\n").encode())
        if capture and "ps" in command and "--services" in command:
            services = () if self.quiesced else self.running_services
            return completed(
                command,
                stdout=(("\n".join(services) + "\n") if services else "").encode(),
            )
        if "stop" in command and "--timeout" in command:
            self.quiesced = True
            return completed(command)
        if "start" in command and any(service in command for service in QUIESCE_SERVICES):
            self.quiesced = False
            return completed(command)
        if "paper_service.data_archive" in command and "create" in command:
            mount = next(
                item for item in command if item.startswith("type=bind,src=")
            )
            backup_dir = Path(mount.split(",dst=", 1)[0].split("src=", 1)[1])
            archive_name = Path(command[command.index("--archive") + 1]).name
            manifest_name = Path(command[command.index("--manifest") + 1]).name
            (backup_dir / archive_name).write_bytes(b"deterministic-data-tar")
            (backup_dir / manifest_name).write_text(
                json.dumps(
                    {
                        "schema": "paper-os-durable-data",
                        "schema_version": 1,
                        "roots": ["library/mobile", "library/raw"],
                        "directories": ["library/mobile", "library/raw"],
                        "files": {},
                        "file_count": 0,
                        "total_bytes": 0,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            return completed(command)
        if command[:3] == ["docker", "container", "ls"]:
            return completed(command, stdout=b"")
        if command[:3] == ["docker", "volume", "inspect"]:
            return completed(command, returncode=1)
        if "pg_isready" in command:
            return completed(command)
        if "pg_restore" in command and "--exit-on-error" in command:
            if self.restored_failure:
                raise OpsError("isolated PostgreSQL restore failed with exit code 1")
            return completed(command)
        if command and command[0] == "rclone" and "redacted" in command:
            return completed(
                command,
                stdout=f"[paper-backup]\ntype = {self.remote_type}\n".encode(),
            )
        if capture and command[-1] == TABLE_QUERY:
            return completed(command, stdout=("\n".join(TABLES) + "\n").encode())
        if capture and command[-1] == COUNT_QUERY:
            return completed(command, stdout=b"18|8\n")
        if capture and command[-1].startswith('SELECT count(*) FROM "'):
            table = command[-1].split('"')[1]
            count = {"papers": 18, "schema_migrations": 8, "sync_jobs": 8}[table]
            return completed(command, stdout=f"{count}\n".encode())
        if capture and command[-1] == PAPER_SHA_QUERY:
            hashes = "\n".join(f"{value:064x}" for value in range(1, 19))
            return completed(command, stdout=(hashes + "\n").encode())
        if capture and command[-1] == INTEGRITY_QUERY:
            return completed(command, stdout=b"0|0\n")
        if capture and command[-1] == CONSTRAINT_QUERY:
            return completed(command, stdout=b"0\n")
        return completed(command)


class ExplodingRunner(CommandRunner):
    def __init__(self):
        self.calls = 0

    def run(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("dry run must not invoke an external command")


class PaperBackupOpsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.service = self.root / "service"
        self.service.mkdir(mode=0o700)
        (self.service / "paper_service").mkdir()
        self.compose = self.service / "compose.yml"
        self.compose.write_text("services: {}\n", encoding="utf-8")
        self.env = self.service / ".env"
        self.env.write_text("PAPER_DB_PASSWORD=never-print-this\n", encoding="utf-8")
        self.env.chmod(0o600)
        self.backups = self.root / "safe" / "paper-backups"

    def paths(self):
        return validate_service_paths(
            str(self.service), str(self.compose), str(self.env), str(self.backups)
        )

    def make_backup(self, backup_id="20260701T120000Z-deadbeef"):
        initialize_backup_root(self.backups)
        directory = self.backups / backup_id
        directory.mkdir(mode=0o700)
        dump = directory / "paper_os.dump"
        dump.write_bytes(b"custom-format-test-dump")
        dump.chmod(0o600)
        snapshot = DatabaseSnapshot(TABLES, 18, 8, 0, 0)
        write_private_json(directory / "backup.json", snapshot.to_json(NOW))
        write_manifest(directory)
        return directory

    def test_paths_are_explicit_private_and_not_broad(self):
        paths = self.paths()
        self.assertEqual(paths.service_root, self.service)
        self.assertEqual(paths.compose_file, self.compose)
        self.assertEqual(paths.env_file, self.env)
        self.assertEqual(paths.backup_root, self.backups)

        with self.assertRaisesRegex(OpsError, "absolute"):
            validate_service_paths(
                "relative", str(self.compose), str(self.env), str(self.backups)
            )
        with self.assertRaisesRegex(OpsError, "inside the service root"):
            outside = self.root / "outside.yml"
            outside.write_text("services: {}\n", encoding="utf-8")
            validate_service_paths(
                str(self.service), str(outside), str(self.env), str(self.backups)
            )
        with self.assertRaisesRegex(OpsError, "too broad"):
            validate_service_paths(
                str(self.service), str(self.compose), str(self.env), "/"
            )
        real_root = self.root / "real-backups"
        real_root.mkdir(mode=0o700)
        linked_root = self.root / "linked-backups"
        linked_root.symlink_to(real_root, target_is_directory=True)
        with self.assertRaisesRegex(OpsError, "not a link"):
            validate_service_paths(
                str(self.service), str(self.compose), str(self.env), str(linked_root)
            )

    def test_environment_file_must_be_private(self):
        self.env.chmod(0o644)
        with self.assertRaisesRegex(OpsError, "600"):
            self.paths()

    def test_dump_command_uses_custom_format_and_explicit_deployment_paths(self):
        command = pg_dump_command(self.paths())
        joined = " ".join(command)
        self.assertIn("--format=custom", command)
        self.assertIn(str(self.compose.parent), command)
        self.assertIn(str(self.compose), command)
        self.assertIn(str(self.env), command)
        self.assertNotIn("never-print-this", joined)
        self.assertNotIn("sh -c", joined)

    def test_backup_is_private_checksummed_and_archive_verified(self):
        runner = FakeRunner()
        result = create_backup(
            self.paths(),
            retention_days=14,
            offsite=False,
            rclone_config=None,
            rclone_remote=None,
            dry_run=False,
            runner=runner,
            now=NOW,
            random_hex="0123abcd",
        )

        self.assertEqual(result.backup_id, "20260829T120000Z-0123abcd")
        metadata = verify_backup_directory(result.backup_dir, self.backups)
        self.assertEqual(metadata["format_version"], 2)
        self.assertEqual(
            metadata["counts"],
            {"papers": 18, "schema_migrations": 8, "sync_jobs": 8},
        )
        self.assertEqual(metadata["consistency"]["mode"], "quiesced")
        self.assertEqual(
            metadata["consistency"]["services"], list(QUIESCE_SERVICES)
        )
        for name in (
            "paper_os.dump",
            "paper_data.tar",
            "paper_data.json",
            "backup.json",
            "SHA256SUMS",
        ):
            self.assertEqual((result.backup_dir / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.backups.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            (self.backups / BACKUP_MARKER_NAME).read_text(encoding="utf-8"),
            BACKUP_MARKER_CONTENT,
        )
        commands = [call["command"] for call in runner.calls]
        self.assertTrue(any("pg_dump" in command for command in commands))
        self.assertTrue(
            any("pg_restore" in command and "--list" in command for command in commands)
        )
        self.assertTrue(any("stop" in command for command in commands))
        self.assertTrue(any("paper_service.data_archive" in command for command in commands))
        self.assertTrue(any("start" in command for command in commands))
        self.assertNotIn("never-print-this", repr(runner.calls))

    def test_backup_records_only_services_that_were_actually_quiesced(self):
        running = ("paper-api", "paper-reader-v2-worker", "paper-worker")
        runner = FakeRunner(running_services=running)
        result = create_backup(
            self.paths(),
            retention_days=14,
            offsite=False,
            rclone_config=None,
            rclone_remote=None,
            dry_run=False,
            runner=runner,
            now=NOW,
            random_hex="0123abce",
        )

        metadata = verify_backup_directory(result.backup_dir, self.backups)
        self.assertEqual(metadata["consistency"]["services"], list(running))

    def test_first_verified_backup_accepts_the_legacy_pre_migration_schema(self):
        class LegacyRunner(FakeRunner):
            def run(self, command, **kwargs):
                if kwargs.get("capture") and list(command)[-1] == TABLE_QUERY:
                    self.calls.append({"command": list(command), **kwargs})
                    return completed(command, stdout=b"papers\nsync_jobs\n")
                return super().run(command, **kwargs)

        result = create_backup(
            self.paths(),
            retention_days=14,
            offsite=False,
            rclone_config=None,
            rclone_remote=None,
            dry_run=False,
            runner=LegacyRunner(),
            now=NOW,
            random_hex="1234abcd",
        )

        metadata = verify_backup_directory(result.backup_dir, self.backups)
        self.assertEqual(metadata["expected_tables"], ["papers", "sync_jobs"])

    def test_changed_inventory_fails_and_removes_only_owned_partial_directory(self):
        class ChangingRunner(FakeRunner):
            def __init__(self):
                super().__init__()
                self.count_calls = 0

            def run(self, command, **kwargs):
                if kwargs.get("capture") and list(command)[-1] == 'SELECT count(*) FROM "papers"':
                    self.count_calls += 1
                    value = b"18\n" if self.count_calls == 1 else b"19\n"
                    self.calls.append({"command": list(command), **kwargs})
                    return completed(command, stdout=value)
                if kwargs.get("capture") and list(command)[-1] == PAPER_SHA_QUERY:
                    maximum = 18 if self.count_calls == 1 else 19
                    hashes = "\n".join(
                        f"{value:064x}" for value in range(1, maximum + 1)
                    )
                    self.calls.append({"command": list(command), **kwargs})
                    return completed(command, stdout=(hashes + "\n").encode())
                return super().run(command, **kwargs)

        runner = ChangingRunner()
        with self.assertRaisesRegex(OpsError, "changed during backup"):
            create_backup(
                self.paths(),
                retention_days=14,
                offsite=False,
                rclone_config=None,
                rclone_remote=None,
                dry_run=False,
                runner=runner,
                now=NOW,
                random_hex="0123abcd",
            )
        self.assertFalse((self.backups / "20260829T120000Z-0123abcd").exists())
        self.assertTrue((self.backups / BACKUP_MARKER_NAME).exists())

    def test_archive_failure_resumes_services_and_removes_partial_backup(self):
        class FailingArchiveRunner(FakeRunner):
            def run(self, command, **kwargs):
                if "paper_service.data_archive" in command and "create" in command:
                    self.calls.append({"command": list(command), **kwargs})
                    raise OpsError("durable paper data archive failed with exit code 1")
                return super().run(command, **kwargs)

        runner = FailingArchiveRunner()
        with self.assertRaisesRegex(OpsError, "durable paper data archive failed"):
            create_backup(
                self.paths(),
                retention_days=14,
                offsite=False,
                rclone_config=None,
                rclone_remote=None,
                dry_run=False,
                runner=runner,
                now=NOW,
                random_hex="0123abcd",
            )
        self.assertFalse(runner.quiesced)
        self.assertTrue(any("start" in call["command"] for call in runner.calls))
        self.assertFalse((self.backups / "20260829T120000Z-0123abcd").exists())

    def test_retention_deletes_only_old_strict_verified_children(self):
        old = self.make_backup()
        recent = self.make_backup("20260828T120000Z-feedface")
        unfamiliar = self.backups / "old-backup"
        unfamiliar.mkdir()
        corrupt = self.backups / "20260601T120000Z-bad0bad0"
        corrupt.mkdir()
        (corrupt / "unexpected").write_text("keep", encoding="utf-8")

        candidates = retention_candidates(self.backups, now=NOW, retention_days=14)
        self.assertEqual(candidates, (old,))
        removed = remove_retention_candidates(candidates, self.backups)

        self.assertEqual(removed, (old.name,))
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(unfamiliar.exists())
        self.assertTrue(corrupt.exists())

    def test_unmarked_nonempty_root_is_never_adopted_for_retention(self):
        self.backups.mkdir(parents=True, mode=0o700)
        self.backups.chmod(0o700)
        (self.backups / "unrelated").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(OpsError, "unmarked non-empty"):
            initialize_backup_root(self.backups)
        self.assertTrue((self.backups / "unrelated").exists())

    def test_dry_run_creates_nothing_and_invokes_nothing(self):
        runner = ExplodingRunner()
        result = create_backup(
            self.paths(),
            retention_days=14,
            offsite=False,
            rclone_config=None,
            rclone_remote=None,
            dry_run=True,
            runner=runner,
            now=NOW,
            random_hex="0123abcd",
        )
        self.assertTrue(result.dry_run)
        self.assertFalse(self.backups.exists())
        self.assertEqual(runner.calls, 0)

        self.backups.mkdir(parents=True, mode=0o700)
        self.backups.chmod(0o700)
        (self.backups / "unrelated").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(OpsError, "unmarked non-empty"):
            create_backup(
                self.paths(),
                retention_days=14,
                offsite=False,
                rclone_config=None,
                rclone_remote=None,
                dry_run=True,
                runner=runner,
                now=NOW,
                random_hex="0123abcd",
            )
        self.assertEqual(runner.calls, 0)

    def test_offsite_requires_specific_private_crypt_configuration(self):
        with self.assertRaisesRegex(OpsError, "non-empty/path"):
            parse_crypt_remote("paper-backup:")
        with self.assertRaisesRegex(OpsError, "traversal-free"):
            parse_crypt_remote("paper-backup:paper-os/../other")

        config = self.root / "rclone.conf"
        config.write_text("[paper-backup]\ntype = crypt\n", encoding="utf-8")
        config.chmod(0o600)
        self.assertEqual(validate_rclone_config(str(config)), config)
        config.chmod(0o644)
        with self.assertRaisesRegex(OpsError, "600"):
            validate_rclone_config(str(config))

        with self.assertRaisesRegex(OpsError, "explicit config"):
            create_backup(
                self.paths(),
                retention_days=14,
                offsite=True,
                rclone_config=None,
                rclone_remote=None,
                dry_run=True,
                runner=ExplodingRunner(),
                now=NOW,
                random_hex="0123abcd",
            )

    def test_remote_type_is_verified_and_manifest_upload_is_last(self):
        config = self.root / "rclone.conf"
        config.write_text("redacted-test\n", encoding="utf-8")
        config.chmod(0o600)
        backup = self.make_backup()

        with self.assertRaisesRegex(OpsError, "not a crypt"):
            verify_crypt_remote(
                FakeRunner(remote_type="drive"),
                config=config,
                remote_name="paper-backup",
            )

        runner = FakeRunner()
        upload_backup(
            runner,
            backup,
            config=config,
            remote="paper-backup:paper-os",
        )
        uploads = [
            call["command"]
            for call in runner.calls
            if call["command"] and "copyto" in call["command"]
        ]
        self.assertEqual(len(uploads), 3)
        self.assertTrue(uploads[-1][-2].endswith("SHA256SUMS"))
        for command in uploads:
            self.assertIn("--ask-password=false", command)
            self.assertIn("--immutable", command)
            self.assertIn("--checksum", command)

    def test_offsite_failure_preserves_the_verified_local_backup(self):
        class FailingUploadRunner(FakeRunner):
            def run(self, command, **kwargs):
                if "copyto" in command:
                    self.calls.append({"command": list(command), **kwargs})
                    raise OpsError("offsite upload failed with exit code 1")
                return super().run(command, **kwargs)

        config = self.root / "rclone.conf"
        config.write_text("[paper-backup]\ntype = crypt\n", encoding="utf-8")
        config.chmod(0o600)
        with self.assertRaisesRegex(OpsError, "offsite upload failed"):
            create_backup(
                self.paths(),
                retention_days=14,
                offsite=True,
                rclone_config=config,
                rclone_remote="paper-backup:paper-os",
                dry_run=False,
                runner=FailingUploadRunner(),
                now=NOW,
                random_hex="0123abcd",
            )
        preserved = self.backups / "20260829T120000Z-0123abcd"
        metadata = verify_backup_directory(preserved, self.backups)
        self.assertEqual(metadata["counts"]["papers"], 18)

    def test_v2_offsite_upload_includes_data_and_writes_manifest_last(self):
        config = self.root / "rclone.conf"
        config.write_text("[paper-backup]\ntype = crypt\n", encoding="utf-8")
        config.chmod(0o600)
        runner = FakeRunner()
        create_backup(
            self.paths(),
            retention_days=14,
            offsite=True,
            rclone_config=config,
            rclone_remote="paper-backup:paper-os",
            dry_run=False,
            runner=runner,
            now=NOW,
            random_hex="0123abcd",
        )
        uploads = [
            call["command"]
            for call in runner.calls
            if "copyto" in call["command"]
        ]
        self.assertEqual(len(uploads), 5)
        self.assertTrue(uploads[-1][-2].endswith("SHA256SUMS"))
        self.assertTrue(any(command[-2].endswith("paper_data.tar") for command in uploads))

    def test_restore_drill_is_pg17_networkless_and_always_cleans_up(self):
        backup = self.make_backup()
        runner = FakeRunner()
        name = restore_drill(
            self.paths(),
            backup_dir=backup,
            dry_run=False,
            runner=runner,
            now=NOW,
            random_hex="0123abcd",
            sleep=lambda _seconds: None,
        )
        self.assertEqual(name, "paper-os-restore-drill-20260829T120000Z-0123abcd")
        commands = [call["command"] for call in runner.calls]
        run = next(command for command in commands if command[:2] == ["docker", "run"])
        self.assertIn("none", run)
        self.assertIn("--network", run)
        self.assertEqual(run[-1], POSTGRES_IMAGE)
        self.assertEqual(commands[-1], ["docker", "rm", "--force", name])

        failing = FakeRunner(restored_failure=True)
        with self.assertRaisesRegex(OpsError, "restore failed"):
            restore_drill(
                self.paths(),
                backup_dir=backup,
                dry_run=False,
                runner=failing,
                now=NOW,
                random_hex="bad0cafe",
                sleep=lambda _seconds: None,
            )
        self.assertEqual(
            failing.calls[-1]["command"],
            [
                "docker",
                "rm",
                "--force",
                "paper-os-restore-drill-20260829T120000Z-bad0cafe",
            ],
        )

    def test_restore_dry_run_verifies_backup_but_does_not_call_docker(self):
        backup = self.make_backup()
        runner = ExplodingRunner()
        name = restore_drill(
            self.paths(),
            backup_dir=backup,
            dry_run=True,
            runner=runner,
            now=NOW,
            random_hex="0123abcd",
        )
        self.assertTrue(name.startswith("paper-os-restore-drill-"))
        self.assertEqual(runner.calls, 0)

    def test_v2_restore_drill_proves_database_and_data_then_cleans_both(self):
        create_runner = FakeRunner()
        created = create_backup(
            self.paths(),
            retention_days=14,
            offsite=False,
            rclone_config=None,
            rclone_remote=None,
            dry_run=False,
            runner=create_runner,
            now=NOW,
            random_hex="0123abcd",
        )
        runner = FakeRunner()
        restore_drill(
            self.paths(),
            backup_dir=created.backup_dir,
            dry_run=False,
            runner=runner,
            now=NOW,
            random_hex="feedface",
            sleep=lambda _seconds: None,
        )
        commands = [call["command"] for call in runner.calls]
        data_verify = next(
            command
            for command in commands
            if "paper_service.data_archive" in command and "verify" in command
        )
        self.assertIn("none", data_verify)
        self.assertIn("--network", data_verify)
        self.assertIn("/backup/paper_data.tar", data_verify)
        self.assertEqual(
            commands[-2][:4], ["docker", "volume", "rm", "--force"]
        )
        self.assertEqual(
            commands[-1],
            [
                "docker",
                "rm",
                "--force",
                "paper-os-restore-drill-20260829T120000Z-feedface",
            ],
        )

    def test_v2_data_restore_failure_cleans_volume_and_database_container(self):
        created = create_backup(
            self.paths(),
            retention_days=14,
            offsite=False,
            rclone_config=None,
            rclone_remote=None,
            dry_run=False,
            runner=FakeRunner(),
            now=NOW,
            random_hex="0123abcd",
        )

        class FailingDataRestoreRunner(FakeRunner):
            def run(self, command, **kwargs):
                if "paper_service.data_archive" in command and "verify" in command:
                    self.calls.append({"command": list(command), **kwargs})
                    raise OpsError("isolated durable data restore failed with exit code 1")
                return super().run(command, **kwargs)

        runner = FailingDataRestoreRunner()
        with self.assertRaisesRegex(OpsError, "durable data restore failed"):
            restore_drill(
                self.paths(),
                backup_dir=created.backup_dir,
                dry_run=False,
                runner=runner,
                now=NOW,
                random_hex="bad0cafe",
                sleep=lambda _seconds: None,
            )
        commands = [call["command"] for call in runner.calls]
        self.assertTrue(any(command[:3] == ["docker", "volume", "rm"] for command in commands))
        self.assertEqual(
            commands[-1],
            [
                "docker",
                "rm",
                "--force",
                "paper-os-restore-drill-20260829T120000Z-bad0cafe",
            ],
        )

    def test_materialized_restore_requires_confirmation_and_preserves_new_volumes(self):
        created = create_backup(
            self.paths(),
            retention_days=14,
            offsite=False,
            rclone_config=None,
            rclone_remote=None,
            dry_run=False,
            runner=FakeRunner(),
            now=NOW,
            random_hex="0123abcd",
        )
        with self.assertRaisesRegex(OpsError, "explicit new-volume confirmation"):
            materialize_restore(
                self.paths(),
                backup_dir=created.backup_dir,
                dry_run=False,
                confirm_create_new_volumes=False,
                runner=ExplodingRunner(),
            )

        dry = materialize_restore(
            self.paths(),
            backup_dir=created.backup_dir,
            dry_run=True,
            confirm_create_new_volumes=False,
            runner=ExplodingRunner(),
        )
        self.assertTrue(dry.dry_run)

        runner = FakeRunner()
        result = materialize_restore(
            self.paths(),
            backup_dir=created.backup_dir,
            dry_run=False,
            confirm_create_new_volumes=True,
            runner=runner,
            sleep=lambda _seconds: None,
        )
        self.assertFalse(result.dry_run)
        commands = [call["command"] for call in runner.calls]
        self.assertTrue(
            any(
                command[:4] == ["docker", "volume", "create", result.database_volume]
                for command in commands
            )
        )
        self.assertTrue(
            any(
                command[:4] == ["docker", "volume", "create", result.data_volume]
                for command in commands
            )
        )
        removed_volumes = {
            command[-1]
            for command in commands
            if command[:3] == ["docker", "volume", "rm"]
        }
        self.assertNotIn(result.database_volume, removed_volumes)
        self.assertNotIn(result.data_volume, removed_volumes)

    def test_failed_materialized_restore_removes_only_new_rollback_volumes(self):
        created = create_backup(
            self.paths(),
            retention_days=14,
            offsite=False,
            rclone_config=None,
            rclone_remote=None,
            dry_run=False,
            runner=FakeRunner(),
            now=NOW,
            random_hex="0123abcd",
        )
        runner = FakeRunner(restored_failure=True)
        with self.assertRaisesRegex(OpsError, "restore failed"):
            materialize_restore(
                self.paths(),
                backup_dir=created.backup_dir,
                dry_run=False,
                confirm_create_new_volumes=True,
                runner=runner,
                sleep=lambda _seconds: None,
            )
        removed = {
            call["command"][-1]
            for call in runner.calls
            if call["command"][:3] == ["docker", "volume", "rm"]
        }
        self.assertEqual(
            removed,
            {
                "paper-os-db-rollback-20260829T120000Z-0123abcd",
                "paper-os-data-rollback-20260829T120000Z-0123abcd",
            },
        )

    def test_systemd_templates_are_explicit_daily_and_not_self_installing(self):
        ops = Path(__file__).resolve().parents[1] / "infra" / "papers" / "ops"
        service = (ops / "paper-os-backup.service").read_text(encoding="utf-8")
        timer = (ops / "paper-os-backup.timer").read_text(encoding="utf-8")
        for required in (
            "--service-root /home/ubuntu/services/paper-os",
            "--compose-file /home/ubuntu/services/paper-os/infra/papers/compose.yml",
            "--env-file /home/ubuntu/services/paper-os/infra/papers/.env",
            "--backup-root /home/ubuntu/services/paper-os/backups/automated",
        ):
            self.assertIn(required, service)
        self.assertIn("OnCalendar=*-*-* 03:30:00 Asia/Hong_Kong", timer)
        self.assertIn("Persistent=true", timer)
        combined = service + timer
        self.assertNotIn("systemctl enable", combined)
        self.assertNotIn("--offsite", service)


if __name__ == "__main__":
    unittest.main()
