import importlib.util
import sys
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from attachments import StagedAttachment
from protocol import authenticated_subprotocol, token_subprotocol


class ConnectorTests(unittest.TestCase):
    def test_manifest_and_pairing_script_exist(self):
        root = Path(__file__).parent
        self.assertTrue((root / "plugin.yaml").is_file())
        self.assertTrue((root / "connect.py").is_file())

    def test_connect_module_loads(self):
        module = _load_connect_module("browser_connect")
        self.assertTrue(callable(module._write_env))

    def test_windows_default_home_uses_local_appdata(self):
        module = _load_connect_module("browser_connect_windows_home")
        env = {"LOCALAPPDATA": r"C:\Users\test\AppData\Local"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                module._hermes_home(platform="nt"),
                Path(r"C:\Users\test\AppData\Local") / "hermes",
            )

    def test_explicit_hermes_home_wins(self):
        module = _load_connect_module("browser_connect_custom_home")
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/custom/hermes"}, clear=True):
            self.assertEqual(module._hermes_home(), Path("/custom/hermes"))

    def test_pairing_token_uses_authenticated_websocket_subprotocol(self):
        token = "a" * 64
        protocol = token_subprotocol(token)
        self.assertEqual(protocol, f"hermes-browser-token.{token}")
        self.assertEqual(authenticated_subprotocol(f"chat, {protocol}", token), protocol)
        self.assertEqual(authenticated_subprotocol("hermes-browser-token.wrong", token), "")

    def test_remote_image_artifacts_reject_local_hosts(self):
        module = _load_adapter()
        self.assertTrue(module._public_image_host("cdn.example.com"))
        self.assertFalse(module._public_image_host("localhost"))
        self.assertFalse(module._public_image_host("127.0.0.1"))
        self.assertFalse(module._public_image_host("192.168.1.20"))

    def test_env_file_is_replaced_without_leaking_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".env"
            path.write_text("KEEP=yes\nHERMES_BROWSER_CONNECTOR_TOKEN=old\n", encoding="utf-8")
            module = _load_connect_module("browser_connect_env")
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(root)}):
                module._write_env({"HERMES_BROWSER_CONNECTOR_TOKEN": "new"})
            self.assertEqual(path.read_text(encoding="utf-8"), "KEEP=yes\nHERMES_BROWSER_CONNECTOR_TOKEN=new\n")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_installers_update_without_force_reinstall(self):
        root = Path(__file__).parent
        powershell = (root / "install.ps1").read_text(encoding="utf-8")
        shell = (root / "install.sh").read_text(encoding="utf-8")
        self.assertIn("git -C $pluginDir fetch", powershell)
        self.assertIn('git -C "$plugin_dir" fetch', shell)
        self.assertIn("remote set-url origin $repository", powershell)
        self.assertIn('remote set-url origin "$repository"', shell)
        self.assertIn("HERMES_BROWSER_CONNECTOR_COMMIT", powershell)
        self.assertIn("HERMES_BROWSER_CONNECTOR_COMMIT", shell)
        self.assertIn("checkout --force $revision", powershell)
        self.assertIn('checkout --force "$revision"', shell)
        self.assertNotIn("checkout --force origin/main", powershell)
        self.assertNotIn('checkout --force origin/main', shell)
        self.assertIn("Test-GitCheckout", powershell)
        self.assertIn("Move-BrokenConnector", powershell)
        self.assertNotIn("rev-parse --is-inside-work-tree *> $null", powershell)
        self.assertIn("plugin-backups", powershell)
        self.assertIn("plugin-backups", shell)
        self.assertIn("ThisisPeggy/Unfold-Hermes-Connector", powershell)
        self.assertIn("ThisisPeggy/Unfold-Hermes-Connector", shell)
        self.assertNotIn("--force }", powershell)
        self.assertNotIn('plugins install "$repository" --enable --force', shell)

    def test_session_list_uses_browser_chat_identity_and_preserves_database_id(self):
        adapter = _load_adapter().BrowserAdapter.__new__(_load_adapter().BrowserAdapter)
        adapter._browser_session_rows = lambda limit=500: [
            {
                "id": "db-session-one",
                "chat_id": "browser-chat-one",
                "title": "First conversation",
                "source": "hermes_browser",
                "message_count": 3,
            },
            {
                "id": "db-session-two",
                "chat_id": "browser-chat-two",
                "title": "Second conversation",
                "source": "hermes_browser",
                "message_count": 5,
            },
        ]

        rows = adapter._list_sessions({"limit": 20})

        self.assertEqual([row["id"] for row in rows], ["browser-chat-one", "browser-chat-two"])
        self.assertEqual(
            [row["history_session_id"] for row in rows],
            ["db-session-one", "db-session-two"],
        )

    def test_session_history_resolves_each_browser_chat_to_its_database_session(self):
        module = _load_adapter()
        adapter = module.BrowserAdapter.__new__(module.BrowserAdapter)
        adapter._browser_session_rows = lambda limit=500: [
            {"id": "db-session-one", "chat_id": "browser-chat-one"},
            {"id": "db-session-two", "chat_id": "browser-chat-two"},
        ]

        class FakeSessionDB:
            def get_messages_as_conversation(self, session_id, include_ancestors=False):
                return [{"role": "assistant", "content": f"history:{session_id}"}]

        adapter._session_db = lambda: FakeSessionDB()

        first = adapter._session_history("browser-chat-one")
        second = adapter._session_history("browser-chat-two")

        self.assertEqual(first[0]["content"], "history:db-session-one")
        self.assertEqual(second[0]["content"], "history:db-session-two")
        self.assertNotEqual(first, second)

    def test_delete_all_sessions_removes_only_browser_database_rows(self):
        module = _load_adapter()
        adapter = module.BrowserAdapter.__new__(module.BrowserAdapter)

        class FakeSessionDB:
            def __init__(self):
                self.deleted = []

            def list_sessions_rich(self, **options):
                self.options = options
                return [
                    {"id": "browser-db-one", "source": "hermes_browser"},
                    {"id": "browser-db-two", "source": "hermes_browser"},
                ]

            def delete_session(self, session_id, sessions_dir=None):
                self.deleted.append((session_id, sessions_dir))
                return True

        database = FakeSessionDB()
        adapter._session_db = lambda read_only=True: database

        deleted = adapter._delete_all_sessions()

        self.assertEqual(deleted, 2)
        self.assertEqual([item[0] for item in database.deleted], ["browser-db-one", "browser-db-two"])
        self.assertEqual(database.options["source"], "hermes_browser")
        self.assertTrue(database.options["include_children"])
        self.assertTrue(database.options["include_archived"])

class ConnectorAttachmentPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_forwards_staged_media_to_the_gateway_event(self):
        module = _load_adapter()
        adapter = module.BrowserAdapter.__new__(module.BrowserAdapter)
        adapter.pending = {}
        adapter.build_source = lambda **_kwargs: object()
        captured = []

        async def handle_message(event):
            captured.append(event)
            adapter.pending["browser-session"]["completion"].set_result("")

        class FakeWebSocket:
            async def send_json(self, _frame):
                return None

        adapter.handle_message = handle_message
        await adapter._prompt(FakeWebSocket(), {
            "session_id": "browser-session",
            "text": "inspect",
            "_attachments": [
                StagedAttachment("cached/image.png", "image/png", 12),
                StagedAttachment("cached/quote.pdf", "application/pdf", 20),
            ],
        })

        self.assertEqual(captured[0].media_urls, ["cached/image.png", "cached/quote.pdf"])
        self.assertEqual(captured[0].media_types, ["image/png", "application/pdf"])

    async def test_generated_image_is_returned_as_a_browser_artifact(self):
        module = _load_adapter()
        adapter = module.BrowserAdapter.__new__(module.BrowserAdapter)
        frames = []

        class FakeWebSocket:
            async def send_json(self, frame):
                frames.append(frame)

        adapter.pending = {"browser-session": {"ws": FakeWebSocket()}}
        result = await adapter._send_image_artifact(
            "browser-session",
            data_url="data:image/png;base64,aGVsbG8=",
            name="result.png",
            mime_type="image/png",
        )

        self.assertTrue(result.success)
        self.assertEqual(frames[0]["params"]["type"], "artifact.image")
        self.assertEqual(frames[0]["params"]["payload"]["name"], "result.png")


def _load_connect_module(name):
    path = Path(__file__).parent / "connect.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_adapter():
    name = "hermes_browser_connector_test_package"
    if f"{name}.adapter" in sys.modules:
        return sys.modules[f"{name}.adapter"]
    root = Path(__file__).parent
    package_spec = importlib.util.spec_from_file_location(
        name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    package = importlib.util.module_from_spec(package_spec)
    sys.modules[name] = package
    package_spec.loader.exec_module(package)
    adapter_spec = importlib.util.spec_from_file_location(f"{name}.adapter", root / "adapter.py")
    module = importlib.util.module_from_spec(adapter_spec)
    sys.modules[f"{name}.adapter"] = module
    adapter_spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    unittest.main()
