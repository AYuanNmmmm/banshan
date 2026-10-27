"""配置读写测试：默认值、局部更新、密钥脱敏。"""
from __future__ import annotations

import copy
import unittest

from panmigrate import config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        config.save(copy.deepcopy(config.DEFAULTS))
        self.cfg = config.load()

    def test_update_is_partial(self):
        config.update({"transfer": {"part_size_mb": 32}})
        cfg = config.load()
        self.assertEqual(cfg["transfer"]["part_size_mb"], 32)
        self.assertEqual(cfg["transfer"]["part_concurrency"],
                         config.DEFAULTS["transfer"]["part_concurrency"],
                         "局部更新不该把同级的其它字段冲掉")

    def test_merge_does_not_filter_keys(self):
        # merge 只负责合并，不做白名单；挡住脏字段的是 App.update_config
        cfg = config.save({"source": {"provider": "mock"}, "没这个键": 1})
        self.assertEqual(cfg["source"]["provider"], "mock")
        self.assertEqual(cfg.get("没这个键"), 1)

    def test_masked_blanks_secrets_but_reports_presence(self):
        config.update({
            "source": {"cookie": "BDUSS=secret", "access_token": "tk"},
            "target": {"refresh_token": "rt", "client_secret": "cs"},
        })
        masked = config.masked(config.load())
        for side in ("source", "target"):
            for key in config.SECRET_KEYS:
                self.assertEqual(masked[side][key], "", f"{side}.{key} 明文泄露给了前端")
        self.assertTrue(masked["source"]["has_cookie"])
        self.assertTrue(masked["source"]["has_token"])
        self.assertTrue(masked["target"]["has_client_secret"])
        self.assertTrue(masked["target"]["has_refresh_token"])

    def test_masked_empty_credentials_report_false(self):
        masked = config.masked(config.load())
        self.assertFalse(masked["source"]["has_token"])
        self.assertFalse(masked["target"]["has_cookie"])

    def test_defaults_include_bandwidth_limit(self):
        self.assertIn("bandwidth_kbps", config.load()["transfer"])


class AccountsTest(unittest.TestCase):
    def setUp(self):
        config.save(copy.deepcopy(config.DEFAULTS))

    def test_snapshot_and_restore_roundtrip(self):
        cfg = config.update({"source": {"provider": "baidu", "cookie": "BDUSS=abc"}})
        config.snapshot_side(cfg, "source")
        cfg["source"]["provider"] = "tianyi"
        config.restore_side(cfg, "source")
        self.assertEqual(cfg["source"]["cookie"], "", "换成天翼后不该继续用百度的 cookie")
        self.assertEqual(cfg["source"]["provider"], "tianyi")

        cfg["source"]["provider"] = "baidu"
        config.restore_side(cfg, "source")
        self.assertEqual(cfg["source"]["cookie"], "BDUSS=abc", "切回百度应自动恢复登录态")

    def test_masked_hides_account_secrets(self):
        cfg = config.update({"source": {"provider": "tianyi", "cookie": "COOKIE_LOGIN_USER=secret"}})
        config.snapshot_side(cfg, "source")
        config.save(cfg)
        masked = config.masked(config.load())
        self.assertEqual(masked["accounts"]["tianyi"]["cookie"], "")
        self.assertEqual(masked["source"]["cookie"], "")

    def test_snapshot_of_empty_account_is_noop(self):
        cfg = config.update({"source": {"provider": "tianyi"}})
        before = len(cfg.get("accounts") or {})
        config.snapshot_side(cfg, "source")
        self.assertEqual(len(cfg.get("accounts") or {}), before)


if __name__ == "__main__":
    unittest.main()
