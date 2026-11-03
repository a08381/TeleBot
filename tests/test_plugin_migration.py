import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.plugin_migration import PluginDataMigrationError, migrate_default_data


class ConfigStub:
    def __init__(self, root, name, key, value):
        self.directory = root / "config" / name
        self.data = {key: value}
        self.saved = []

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value
        self.saved.append((key, value))


class PluginDataMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "config").mkdir()

    def e5_config(self, value="config/e5_accounts.sqlite3"):
        return ConfigStub(self.root, "e5", "database_path", value)

    def wm_config(self, value="config/wm_sessions.json"):
        return ConfigStub(self.root, "wm", "session_file", value)

    def migrate_e5(self, cfg):
        return migrate_default_data(cfg, "database_path", "e5_accounts.sqlite3", sqlite=True)

    def migrate_wm(self, cfg):
        return migrate_default_data(
            cfg, "session_file", "wm_sessions.json", legacy_filenames=("wm_session.json",)
        )

    def make_database(self, path):
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE fixture (value TEXT)")
            connection.execute("INSERT INTO fixture VALUES ('preserved')")
            connection.commit()
        finally:
            connection.close()

    def test_sqlite_moves_before_setting_changes_and_preserves_filename(self):
        source = self.root / "config/e5_accounts.sqlite3"
        target = self.root / "config/e5/e5_accounts.sqlite3"
        self.make_database(source)
        original = source.read_bytes()
        cfg = self.e5_config()
        save = cfg.set

        def check_before_save(key, value):
            self.assertFalse(source.exists())
            self.assertEqual(target.read_bytes(), original)
            save(key, value)

        with patch.object(cfg, "set", side_effect=check_before_save):
            self.assertEqual(self.migrate_e5(cfg), str(target))
        self.assertEqual(cfg.get("database_path"), "config/e5/e5_accounts.sqlite3")
        with sqlite3.connect(target) as connection:
            self.assertEqual(connection.execute("SELECT value FROM fixture").fetchone(), ("preserved",))
        self.assertEqual(self.migrate_e5(cfg), str(target))
        self.assertEqual(len(cfg.saved), 1)

    def test_equivalent_default_paths_are_recognized(self):
        for plugin, filename, key in (("e5", "e5_accounts.sqlite3", "database_path"),
                                      ("wm", "wm_sessions.json", "session_file")):
            source = self.root / "config" / filename
            target = self.root / "config" / plugin / filename
            values = [f"config/{filename}", f"./config/{filename}",
                      f".\\config\\{filename}", f"config/../config/{filename}",
                      str(source), str(source).replace("/", "\\"),
                      f"  config/{filename}  ", f"config/{plugin}/{filename}", ""]
            for value in values:
                with self.subTest(plugin=plugin, value=value):
                    if target.exists():
                        target.unlink()
                    source.write_bytes(b"fixture-data")
                    cfg = ConfigStub(self.root, plugin, key, value)
                    self.assertEqual(
                        migrate_default_data(cfg, key, filename, sqlite=plugin == "e5"), str(target)
                    )
                    self.assertEqual(target.read_bytes(), b"fixture-data")
                    self.assertFalse(source.exists())
                    self.assertEqual(cfg.get(key), f"config/{plugin}/{filename}")

    def test_custom_paths_are_unchanged_and_skip_default_files(self):
        source = self.root / "config/e5_accounts.sqlite3"
        source.write_bytes(b"old-data")
        for value in ("storage/e5_accounts.sqlite3", "e5_accounts.sqlite3",
                      "storage\\database.sqlite3", str(self.root / "custom.sqlite3"),
                      "  custom.sqlite3  "):
            with self.subTest(value=value):
                cfg = self.e5_config(value)
                self.assertEqual(self.migrate_e5(cfg), value)
                self.assertEqual(cfg.saved, [])
                self.assertEqual(source.read_bytes(), b"old-data")
                self.assertFalse(cfg.directory.exists())
        wm_source = self.root / "config/wm_sessions.json"
        wm_source.write_text("{}", encoding="utf-8")
        cfg = self.wm_config("custom\\wm_sessions.json")
        self.assertEqual(self.migrate_wm(cfg), "custom\\wm_sessions.json")
        self.assertTrue(wm_source.exists())

    def test_missing_data_only_updates_default_setting(self):
        cfg = self.e5_config()
        target = Path(self.migrate_e5(cfg))
        self.assertEqual(target, self.root / "config/e5/e5_accounts.sqlite3")
        self.assertFalse(target.exists())
        self.assertEqual(cfg.get("database_path"), "config/e5/e5_accounts.sqlite3")

    def test_conflicting_data_is_never_replaced(self):
        cfg = self.e5_config()
        cfg.directory.mkdir()
        source = self.root / "config/e5_accounts.sqlite3"
        target = cfg.directory / source.name
        source.write_bytes(b"old")
        target.write_bytes(b"new")
        with self.assertRaisesRegex(PluginDataMigrationError, "冲突"):
            self.migrate_e5(cfg)
        self.assertEqual(source.read_bytes(), b"old")
        self.assertEqual(target.read_bytes(), b"new")
        self.assertEqual(cfg.saved, [])

    def test_existing_destination_without_source_is_reused(self):
        cfg = self.e5_config()
        cfg.directory.mkdir()
        target = cfg.directory / "e5_accounts.sqlite3"
        target.write_bytes(b"existing-data")
        self.assertEqual(self.migrate_e5(cfg), str(target))
        self.assertEqual(target.read_bytes(), b"existing-data")

    def test_all_sqlite_sidecar_files_move_byte_for_byte(self):
        cfg = self.e5_config()
        originals = {}
        for suffix in ("", "-wal", "-shm", "-journal"):
            source = self.root / "config" / ("e5_accounts.sqlite3" + suffix)
            originals[source.name] = b"fixture" + suffix.encode()
            source.write_bytes(originals[source.name])
        self.migrate_e5(cfg)
        for name, data in originals.items():
            self.assertFalse((self.root / "config" / name).exists())
            self.assertEqual((cfg.directory / name).read_bytes(), data)

    def test_committed_wal_data_survives_offline_migration(self):
        writer = self.root / "writer.sqlite3"
        connection = sqlite3.connect(writer)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("CREATE TABLE fixture (value TEXT)")
            connection.execute("INSERT INTO fixture VALUES ('wal-preserved')")
            connection.commit()
            # 复制出异常退出后的文件状态，不实际终止进程。
            for suffix in ("", "-wal", "-shm"):
                shutil.copy2(Path(str(writer) + suffix), self.root / "config" / ("e5_accounts.sqlite3" + suffix))
        finally:
            connection.close()
        target = self.migrate_e5(self.e5_config())
        connection = sqlite3.connect(target)
        try:
            self.assertEqual(connection.execute("SELECT value FROM fixture").fetchone(), ("wal-preserved",))
        finally:
            connection.close()

    def test_orphan_sidecars_stop_initialization(self):
        for side in ("source", "target"):
            with self.subTest(side=side):
                cfg = self.e5_config()
                base = self.root / "config" if side == "source" else cfg.directory
                base.mkdir(exist_ok=True)
                orphan = base / "e5_accounts.sqlite3-wal"
                orphan.write_bytes(b"wal-data")
                with self.assertRaisesRegex(PluginDataMigrationError, "缺少主数据库"):
                    self.migrate_e5(cfg)
                self.assertEqual(orphan.read_bytes(), b"wal-data")
                self.assertFalse((cfg.directory / "e5_accounts.sqlite3").exists())
                self.assertEqual(cfg.saved, [])
                orphan.unlink()

    def test_destination_sidecar_conflict_keeps_source(self):
        cfg = self.e5_config()
        cfg.directory.mkdir()
        source = self.root / "config/e5_accounts.sqlite3"
        source.write_bytes(b"source")
        target = cfg.directory / "e5_accounts.sqlite3-shm"
        target.write_bytes(b"target")
        with self.assertRaisesRegex(PluginDataMigrationError, "冲突"):
            self.migrate_e5(cfg)
        self.assertEqual(source.read_bytes(), b"source")
        self.assertEqual(target.read_bytes(), b"target")
        self.assertEqual(cfg.saved, [])

    def test_busy_source_stops_before_any_move(self):
        cfg = self.e5_config()
        source = self.root / "config/e5_accounts.sqlite3"
        source.write_bytes(b"original")
        with patch("utils.plugin_migration._exclusive_source", side_effect=PermissionError("busy")):
            with self.assertRaises(PluginDataMigrationError):
                self.migrate_e5(cfg)
        self.assertEqual(source.read_bytes(), b"original")
        self.assertFalse((cfg.directory / source.name).exists())
        self.assertEqual(cfg.saved, [])

    def test_active_sqlite_transaction_blocks_migration(self):
        source = self.root / "config/e5_accounts.sqlite3"
        self.make_database(source)
        script = (
            "import sqlite3,sys\n"
            "connection=sqlite3.connect(sys.argv[1])\n"
            "connection.execute('BEGIN EXCLUSIVE')\n"
            "print('locked', flush=True)\n"
            "sys.stdin.readline()\n"
            "connection.rollback()\n"
            "connection.close()\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(source)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            cfg = self.e5_config()
            with self.assertRaises(PluginDataMigrationError):
                self.migrate_e5(cfg)
            self.assertTrue(source.exists())
            self.assertFalse((cfg.directory / source.name).exists())
            self.assertEqual(cfg.saved, [])
        finally:
            process.communicate("close\n", timeout=10)
        self.assertEqual(process.returncode, 0)

    @unittest.skipUnless(os.name == "nt", "Windows 独占文件句柄")
    def test_idle_sqlite_connection_and_open_session_file_block_migration(self):
        cfg = self.e5_config()
        source = self.root / "config/e5_accounts.sqlite3"
        self.make_database(source)
        connection = sqlite3.connect(source)
        try:
            with self.assertRaises(PluginDataMigrationError):
                self.migrate_e5(cfg)
        finally:
            connection.close()
        cfg = self.wm_config()
        source = self.root / "config/wm_sessions.json"
        source.write_bytes(b"{}")
        with source.open("rb"):
            with self.assertRaises(PluginDataMigrationError):
                self.migrate_wm(cfg)
        self.assertEqual(source.read_bytes(), b"{}")
        self.assertEqual(cfg.saved, [])

    def test_publish_failure_keeps_original_family_and_removes_partial_targets(self):
        cfg = self.e5_config()
        source = self.root / "config/e5_accounts.sqlite3"
        sidecar = Path(str(source) + "-wal")
        source.write_bytes(b"database")
        sidecar.write_bytes(b"wal")
        link = os.link

        def fail_second_publish(copy, target):
            if str(target).endswith("-wal"):
                raise OSError("simulated publish failure")
            return link(copy, target)

        with patch("utils.plugin_migration.os.link", side_effect=fail_second_publish):
            with self.assertRaises(PluginDataMigrationError):
                self.migrate_e5(cfg)
        self.assertEqual(source.read_bytes(), b"database")
        self.assertEqual(sidecar.read_bytes(), b"wal")
        self.assertEqual(list(cfg.directory.iterdir()), [])
        self.assertEqual(cfg.saved, [])

    def test_destination_created_during_publish_is_not_overwritten(self):
        cfg = self.wm_config()
        source = self.root / "config/wm_sessions.json"
        source.write_bytes(b"original")
        link = os.link

        def race_publish(copy, target):
            target.write_bytes(b"concurrent-target")
            return link(copy, target)

        with patch("utils.plugin_migration.os.link", side_effect=race_publish):
            with self.assertRaises(PluginDataMigrationError):
                self.migrate_wm(cfg)
        self.assertEqual(source.read_bytes(), b"original")
        self.assertEqual((cfg.directory / source.name).read_bytes(), b"concurrent-target")
        self.assertEqual(cfg.saved, [])

    def test_cleanup_failure_preserves_complete_destination_and_blocks_retry(self):
        cfg = self.e5_config()
        source = self.root / "config/e5_accounts.sqlite3"
        sidecar = Path(str(source) + "-wal")
        source.write_bytes(b"database")
        sidecar.write_bytes(b"wal")
        unlink = Path.unlink

        def fail_source_cleanup(path, *args, **kwargs):
            if path == source:
                raise PermissionError("simulated cleanup failure")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_source_cleanup):
            with self.assertRaises(PluginDataMigrationError):
                self.migrate_e5(cfg)
        self.assertEqual((cfg.directory / source.name).read_bytes(), b"database")
        self.assertEqual((cfg.directory / sidecar.name).read_bytes(), b"wal")
        self.assertTrue(source.exists())
        self.assertEqual(cfg.saved, [])
        with self.assertRaisesRegex(PluginDataMigrationError, "冲突"):
            self.migrate_e5(cfg)

    def test_configuration_save_failure_retries_using_migrated_data(self):
        cfg = self.wm_config()
        source = self.root / "config/wm_sessions.json"
        source.write_bytes(b"{}")
        with patch.object(cfg, "set", side_effect=OSError("configuration write failed")):
            with self.assertRaises(OSError):
                self.migrate_wm(cfg)
        self.assertFalse(source.exists())
        self.assertEqual((cfg.directory / source.name).read_bytes(), b"{}")
        self.assertEqual(self.migrate_wm(cfg), str(cfg.directory / source.name))
        self.assertEqual(cfg.get("session_file"), "config/wm/wm_sessions.json")

    def test_legacy_wm_single_account_archives_without_renaming_or_guessing_owner(self):
        cfg = self.wm_config("config\\wm_session.json")
        single = self.root / "config/wm_session.json"
        multi = self.root / "config/wm_sessions.json"
        single.write_text(json.dumps({"token": "fixture-single"}), encoding="utf-8")
        multi.write_text(json.dumps({"sessions": {"101": {"token": "fixture-multi"}}}), encoding="utf-8")
        originals = {path.name: path.read_bytes() for path in (single, multi)}
        self.assertEqual(self.migrate_wm(cfg), str(cfg.directory / multi.name))
        for name, data in originals.items():
            self.assertFalse((self.root / "config" / name).exists())
            self.assertEqual((cfg.directory / name).read_bytes(), data)
        self.assertEqual(cfg.get("session_file"), "config/wm/wm_sessions.json")
        self.assertEqual(self.migrate_wm(cfg), str(cfg.directory / multi.name))

    def test_single_account_only_does_not_become_multi_account_store(self):
        cfg = self.wm_config("config/wm_session.json")
        source = self.root / "config/wm_session.json"
        source.write_bytes(b'{"token": "fixture-single"}')
        target = Path(self.migrate_wm(cfg))
        self.assertFalse(target.exists())
        self.assertEqual((cfg.directory / source.name).read_bytes(), b'{"token": "fixture-single"}')
        self.assertEqual(cfg.get("session_file"), "config/wm/wm_sessions.json")

    def test_wm_archive_conflict_is_checked_before_moving_multi_account_store(self):
        cfg = self.wm_config()
        cfg.directory.mkdir()
        for name in ("wm_sessions.json", "wm_session.json"):
            (self.root / "config" / name).write_bytes(b"source")
        (cfg.directory / "wm_session.json").write_bytes(b"target")
        with self.assertRaisesRegex(PluginDataMigrationError, "冲突"):
            self.migrate_wm(cfg)
        self.assertEqual((self.root / "config/wm_sessions.json").read_bytes(), b"source")
        self.assertFalse((cfg.directory / "wm_sessions.json").exists())
        self.assertEqual(cfg.saved, [])


if __name__ == "__main__":
    unittest.main()
