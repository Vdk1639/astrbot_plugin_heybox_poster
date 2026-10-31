"""旧配置迁移与登录退出后的持久行为。"""

import copy
import pathlib
import shutil
import sys
import unittest
import uuid
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_heybox_poster.core.migration import migrate_credentials, migrate_settings
from astrbot_plugin_heybox_poster.xiaoheihe.auth import CredentialStore
from astrbot_plugin_heybox_poster.xiaoheihe.models import Credentials


class Config(dict):
    fail = False

    def save_config(self):
        if self.fail:
            raise OSError("readonly config")
        self.saved = copy.deepcopy(dict(self))


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / ".test-runs" / uuid.uuid4().hex
        self.directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)
        self.store = CredentialStore(self.directory)
        self.config = Config(cookie="user_pkey=p; x_xhh_tokenid=t", heybox_id="123", default_topic_ids="43", dry_run=False)

    def test_legacy_settings_and_credentials_migrate_once(self):
        migrate_settings(self.config, self.directory)
        self.assertEqual(self.config["publishing"]["default_topic_ids"], "43")
        self.assertFalse(self.config["publishing"]["dry_run"])
        self.assertEqual(self.config.saved["publishing"]["default_topic_ids"], "43")
        self.assertFalse(self.config.saved["publishing"]["dry_run"])
        self.assertEqual(migrate_credentials(self.config, self.store, self.directory), "")
        self.assertEqual(self.store.load("default").uid, "123")
        self.assertEqual(self.config.saved["cookie"], "")
        self.assertTrue((self.directory / "legacy_login_backup.json").exists())
        self.assertTrue((self.directory / "settings_migrated.json").exists())
        self.store.delete("default")
        self.config["cookie"] = "user_pkey=old; x_xhh_tokenid=old"
        self.config["publishing"]["default_topic_ids"] = "44"
        migrate_settings(self.config, self.directory)
        migrate_credentials(self.config, self.store, self.directory)
        self.assertIsNone(self.store.load("default"))
        self.assertEqual(self.config["publishing"]["default_topic_ids"], "44")

    def test_existing_profile_is_not_replaced_by_old_config(self):
        self.store.save(Credentials("default", "456", "新账号", {"user_pkey": "new"}))
        migrate_credentials(self.config, self.store, self.directory)
        self.assertEqual(self.store.load("default").uid, "456")

    def test_new_settings_survive_schema_injected_legacy_defaults(self):
        config = Config(dry_run=True, view_limit=2, publishing={"dry_run": False, "view_limit": 3})
        migrate_settings(config, self.directory)
        self.assertFalse(config["publishing"]["dry_run"])
        self.assertEqual(config["publishing"]["view_limit"], 3)

    def test_settings_marker_prevents_legacy_overwrite_after_credential_retry(self):
        migrate_settings(self.config, self.directory)
        self.config["publishing"]["dry_run"] = False
        self.config["cookie"] = ""
        migrate_settings(self.config, self.directory)
        self.assertFalse(self.config["publishing"]["dry_run"])

    def test_legacy_cookie_identity_conflict_is_not_imported(self):
        self.config["cookie"] += "; heybox_id=456"
        self.assertIn("身份", migrate_credentials(self.config, self.store, self.directory))
        self.assertIsNone(self.store.load("default"))

    def test_missing_identity_preserves_original_until_manual_login(self):
        self.config["heybox_id"] = ""
        before = dict(self.config)
        self.assertIn("不完整", migrate_credentials(self.config, self.store, self.directory))
        self.assertEqual(dict(self.config), before)
        self.assertIsNone(self.store.load("default"))
        self.assertFalse((self.directory / "integration_migrated.json").exists())
        self.store.save(Credentials("default", "456", "新账号", {"user_pkey": "new"}))
        self.assertEqual(migrate_credentials(self.config, self.store, self.directory), "")

    def test_config_write_failure_rolls_back_and_does_not_mark_complete(self):
        self.config.fail = True
        before = dict(self.config)
        with self.assertRaises(OSError):
            migrate_credentials(self.config, self.store, self.directory)
        self.assertEqual(dict(self.config), before)
        self.assertIsNone(self.store.load("default"))
        self.assertFalse((self.directory / "integration_migrated.json").exists())

    def test_migration_failure_preserves_existing_profile(self):
        self.store.save(Credentials("default", "456", "新账号", {"user_pkey": "new"}))
        self.config.fail = True
        with self.assertRaises(OSError):
            migrate_credentials(self.config, self.store, self.directory)
        self.assertEqual(self.store.load("default").uid, "456")

    def test_marker_write_failure_restores_persisted_config(self):
        from astrbot_plugin_heybox_poster.core import migration
        original_write = migration.atomic_write_json
        def write(path, data):
            if path.name == "integration_migrated.json":
                raise OSError("marker write failed")
            original_write(path, data)
        with patch.object(migration, "atomic_write_json", side_effect=write), self.assertRaises(OSError):
            migrate_credentials(self.config, self.store, self.directory)
        self.assertEqual(self.config.saved["cookie"], "user_pkey=p; x_xhh_tokenid=t")
        self.assertIsNone(self.store.load("default"))
