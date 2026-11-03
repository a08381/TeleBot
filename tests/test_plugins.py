import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class PluginLoadingTests(unittest.TestCase):
    def run_isolated(self, script):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            isolated = Path(directory)
            for package in ("core", "plugins", "utils"):
                shutil.copytree(root / package, isolated / package, ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy2(root / "main.py", isolated / "main.py")
            result = subprocess.run(
                [sys.executable, "-c", textwrap.dedent(script)], cwd=isolated,
                env={**os.environ, "PYTHONUTF8": "1"},
                capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_private_utilities_do_not_initialize_plugins_on_import(self):
        self.run_isolated("""
            import sys
            from pathlib import Path
            import plugins.e5.e5_renewal
            import plugins.wm.wm_api
            import plugins.wm.wm_items
            import plugins.yiff.http_pool
            import plugins.yiff.posts_pool
            assert not Path("config").exists()
            for name in ("e5", "wm", "yiff"):
                assert f"plugins.{name}.{name}" not in sys.modules
                assert f"plugins.{name}.handlers" not in sys.modules
        """)

    def test_all_plugins_initialize_and_fully_reload_without_network(self):
        self.run_isolated("""
            import asyncio
            import sys
            from pathlib import Path
            from unittest.mock import patch

            import main
            from core.dispatcher import Listeners, Buttons
            from core.lifecycle import hook_names
            from core.loader import iter_plugin_modules, reload_all_plugins
            from core.webhook import WEBHOOK_BOT_KEY, webhook_routes
            from utils.plugin_config import all_plugin_configs

            async def exercise():
                with patch("socket.socket.connect", side_effect=AssertionError("测试不应连接网络")):
                    main.main(["--init-config"])
                    expected = sorted(iter_plugin_modules())
                    assert len(expected) == 7, expected
                    assert all(name in sys.modules for name in expected)
                    assert set(all_plugin_configs()) == {"yiff", "wm", "e5"}
                    assert "check" in Listeners
                    assert "/e5/callback" in webhook_routes()
                    assert Path("config/e5/e5_accounts.sqlite3").exists()
                    assert not Path("config.json").exists()
                    assert not Path("plugins/config").exists()
                    assert not Path("config/handlers.json").exists()
                    for name in ("e5", "wm", "yiff"):
                        assert Path(f"config/{name}/config.json").exists()
                        assert not Path(f"config/{name}.json").exists()
                        assert f"plugins.{name}.{name}" in sys.modules
                        assert f"plugins.{name}.handlers" not in sys.modules

                    old_modules = {name: module for name, module in sys.modules.items() if name.startswith("plugins.")}
                    from plugins.e5.e5 import service
                    before = (
                        {name: len(handlers) for name, handlers in Listeners.items()},
                        {name: len(handlers) for name, handlers in Buttons.items()},
                        hook_names(),
                    )
                    assert sorted(await reload_all_plugins()) == expected
                    after = (
                        {name: len(handlers) for name, handlers in Listeners.items()},
                        {name: len(handlers) for name, handlers in Buttons.items()},
                        hook_names(),
                    )
                    assert before == after
                    assert all(sys.modules[name] is not module for name, module in old_modules.items())
                    from plugins.e5 import e5
                    assert e5.service is not service
                    assert e5.WEBHOOK_BOT_KEY is WEBHOOK_BOT_KEY
                    assert e5.service._task is None
                    assert "utils.e5_renewal" not in sys.modules
            asyncio.run(exercise())
        """)

    def test_legacy_configuration_and_database_migrate_before_service_initialization(self):
        self.run_isolated("""
            import asyncio
            import json
            from pathlib import Path
            from unittest.mock import patch

            from plugins.e5.e5_renewal import E5RenewalStore
            from core.loader import load_all_plugins, reload_all_plugins
            from utils.plugin_config import get_plugin_config

            root = Path.cwd()
            Path("config").mkdir()
            legacy_database = root / "config/e5_accounts.sqlite3"
            store = E5RenewalStore(legacy_database)
            store.save_pending(101, "test-client", "test-secret", "keep-state")
            for name, values in {
                "e5": {"database_path": "config/e5_accounts.sqlite3", "interval_hours": 48},
                "wm": {"session_file": "config/wm_sessions.json", "warm_items": False},
                "yiff": {"site": "https://site.example.test", "default_tags": ["test"]},
            }.items():
                Path(f"config/{name}.json").write_text(json.dumps(values), encoding="utf-8")

            async def exercise():
                with patch("socket.socket.connect", side_effect=AssertionError("测试不应连接网络")):
                    loaded = load_all_plugins()
                    assert len(loaded) == 7, loaded
                    from plugins.e5.e5 import service
                    assert service.store.get_pending(101)["state"] == "keep-state"
                    assert service._task is None
                    assert not legacy_database.exists()
                    assert Path("config/e5/e5_accounts.sqlite3").exists()
                    for name in ("e5", "wm", "yiff"):
                        assert not Path(f"config/{name}.json").exists()
                        cfg = get_plugin_config(name)
                        assert cfg.path == root / "config" / name / "config.json"
                        assert cfg.directory == cfg.path.parent
                    assert get_plugin_config("e5").interval_hours == 48
                    assert get_plugin_config("yiff").site == "https://site.example.test"
                    assert get_plugin_config("yiff").default_tags == ["test"]
                    assert get_plugin_config("wm").warm_items is False
                    assert get_plugin_config("e5").database_path == "config/e5/e5_accounts.sqlite3"
                    assert get_plugin_config("wm").session_file == "config/wm/wm_sessions.json"
                    assert await reload_all_plugins() == loaded
                    from plugins.e5.e5 import service as reloaded
                    assert reloaded.store.get_pending(101)["state"] == "keep-state"
            asyncio.run(exercise())
        """)

    def test_reload_closes_old_resources_restarts_tasks_and_preserves_accounts(self):
        self.run_isolated("""
            import asyncio
            import importlib
            from pathlib import Path
            from types import SimpleNamespace
            from unittest.mock import AsyncMock, patch

            from core.loader import load_all_plugins, reload_all_plugins, start_plugins, stop_plugins
            from utils.plugin_config import get_plugin_config

            async def exercise():
                clients = []
                def create_client(**kwargs):
                    response = SimpleNamespace(status_code=200, json=lambda: {"posts": []})
                    client = SimpleNamespace(request=AsyncMock(return_value=response), aclose=AsyncMock())
                    clients.append(client)
                    return client

                with patch("socket.socket.connect", side_effect=AssertionError("测试不应连接网络")), patch("utils.browser_profile.create_client", create_client):
                    load_all_plugins()
                    get_plugin_config("wm").set("warm_items", False)
                    await reload_all_plugins()
                    app = SimpleNamespace(bot=AsyncMock())
                    await start_plugins(app)
                    try:
                        from plugins.e5.e5 import service as old_service
                        from plugins.wm import wm_api as old_wm
                        from plugins.yiff import posts_pool as old_posts, http_pool as old_http
                        old_scheduler = old_service._task
                        old_pool = old_posts.get_pool()
                        old_pool_task = old_pool._task
                        old_site_client = await old_http.http()
                        old_wm_client = await old_wm.wm()
                        socket = SimpleNamespace(close=AsyncMock())
                        old_wm_client._status_sockets[101] = socket
                        old_wm_client.store.put(old_wm.WMSession(tg_user_id=101, token="test-jwt"))
                        old_service.store.save_pending(101, "test-client", "test-secret", "test-state")
                        get_plugin_config("yiff").set("site", "https://new.example.test")
                        await reload_all_plugins()

                        from plugins.e5.e5 import service as new_service
                        from plugins.wm import wm_api as new_wm
                        from plugins.yiff import posts_pool as new_posts, http_pool as new_http
                        assert old_scheduler.done()
                        assert old_pool_task.done()
                        assert not old_site_client.started
                        clients[0].aclose.assert_awaited_once()
                        socket.close.assert_awaited_once()
                        assert old_posts.get_pool() is None
                        assert old_wm.current() is None
                        assert new_service is not old_service
                        assert new_service._task is not old_scheduler and not new_service._task.done()
                        assert new_posts.get_pool() is not old_pool
                        assert new_posts.get_pool()._cfg.site == "https://new.example.test"
                        assert (await new_http.http()) is not old_site_client
                        assert (await new_wm.wm()).store.get(101).token == "test-jwt"
                        assert new_service.store.get_pending(101)["state"] == "test-state"
                        assert Path("config/e5/e5_accounts.sqlite3").exists()
                    finally:
                        await stop_plugins()
                    assert new_service._task is None
            asyncio.run(exercise())
        """)
