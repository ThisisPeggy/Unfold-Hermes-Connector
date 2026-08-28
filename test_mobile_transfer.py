import base64
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
import importlib.util
from urllib.parse import parse_qs, urlsplit

from aiohttp import ClientSession, FormData


def _load_mobile_transfer():
    package_name = "hermes_browser_mobile_transfer_test_package"
    module_name = f"{package_name}.mobile_transfer"
    if module_name in sys.modules:
        return sys.modules[module_name]
    root = Path(__file__).parent
    package_spec = importlib.util.spec_from_file_location(
        package_name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    package = importlib.util.module_from_spec(package_spec)
    sys.modules[package_name] = package
    package_spec.loader.exec_module(package)
    spec = importlib.util.spec_from_file_location(module_name, root / "mobile_transfer.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


mobile_transfer = _load_mobile_transfer()
attachments = sys.modules[f"{mobile_transfer.__package__}.attachments"]


class UploadedAttachmentValidationTests(unittest.TestCase):
    def test_image_bytes_choose_the_canonical_mime(self):
        name, mime_type = attachments.validate_uploaded_attachment(
            "../../camera.jpg",
            "application/octet-stream",
            b"\xff\xd8\xffphone-photo",
        )
        self.assertEqual(name, "camera.jpg")
        self.assertEqual(mime_type, "image/jpeg")

    def test_fake_image_and_executable_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid image"):
            attachments.validate_uploaded_attachment("photo.png", "image/png", b"not-an-image")
        with self.assertRaisesRegex(ValueError, "unsupported file type"):
            attachments.validate_uploaded_attachment("payload.exe", "application/octet-stream", b"MZ")

    def test_transcoded_phone_images_and_common_text_mimes_are_normalized(self):
        name, mime_type = attachments.validate_uploaded_attachment(
            "camera.heic",
            "image/heic",
            b"\xff\xd8\xffbrowser-transcoded-jpeg",
        )
        self.assertEqual(name, "camera.jpg")
        self.assertEqual(mime_type, "image/jpeg")
        name, mime_type = attachments.validate_uploaded_attachment(
            "notes.json",
            "application/json",
            b'{"safe": true}',
        )
        self.assertEqual((name, mime_type), ("notes.json", "text/plain"))


class MobileTransferServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []

        async def emit(owner, kind, payload):
            self.events.append((owner, kind, payload))

        self.service = mobile_transfer.MobileTransferService(
            emit,
            port=0,
            bind_host="127.0.0.1",
            advertised_host="192.168.1.25",
            ttl_seconds=30,
        )

    async def asyncTearDown(self):
        await self.service.close()

    async def test_create_uses_fragment_token_and_cancel_stops_the_listener(self):
        owner = object()
        result = await self.service.create(owner)

        self.assertIn("http://192.168.1.25:", result["url"])
        self.assertIn("#token=", result["url"])
        self.assertNotIn("?token=", result["url"])
        self.assertEqual(result["ttl_seconds"], 30)
        self.assertIsNotNone(self.service._runner)

        cancelled = await self.service.cancel(owner, result["transfer_id"])
        self.assertTrue(cancelled["cancelled"])
        self.assertIsNone(self.service._runner)

    async def test_authorization_is_owner_channel_specific(self):
        result = await self.service.create(object())
        transfer = self.service._transfers[result["transfer_id"]]
        request = SimpleNamespace(
            match_info={"transfer_id": transfer.transfer_id},
            headers={"Authorization": f"Bearer {transfer.token}"},
        )
        self.assertIs(self.service._authorized_transfer(request), transfer)
        request.headers["Authorization"] = "Bearer wrong"
        with self.assertRaises(mobile_transfer.web.HTTPUnauthorized):
            self.service._authorized_transfer(request)

    async def test_expired_channel_emits_event_and_closes(self):
        owner = object()
        result = await self.service.create(owner)
        transfer = self.service._transfers[result["transfer_id"]]
        transfer.expires_at = time.time() - 1
        transfer.expiry_task.cancel()
        await self.service._expire(transfer.transfer_id, 0)

        self.assertEqual(self.events[0][1], "mobile_transfer.expired")
        self.assertEqual(self.events[0][2]["transfer_id"], transfer.transfer_id)
        self.assertIsNone(self.service._runner)

    async def test_authenticated_http_upload_reaches_only_the_owner_websocket(self):
        await self.service.close()
        self.service.advertised_host = "127.0.0.1"
        owner = object()
        result = await self.service.create(owner)
        parsed = urlsplit(result["url"])
        token = parse_qs(parsed.fragment)["token"][0]
        base = f"http://127.0.0.1:{self.service.port}{parsed.path}"
        headers = {"Authorization": f"Bearer {token}"}
        png = b"\x89PNG\r\n\x1a\nphone-photo"
        form = FormData()
        form.add_field("file", png, filename="phone.png", content_type="image/png")

        async with ClientSession() as session:
            async with session.get(f"{base}/status", headers=headers) as response:
                self.assertEqual(response.status, 200)
            async with session.post(f"{base}/files", data=form, headers=headers) as response:
                self.assertEqual(response.status, 200)
            async with session.get(f"{base}/status", headers={"Authorization": "Bearer wrong"}) as response:
                self.assertEqual(response.status, 401)

        self.assertEqual(self.events[0][0], owner)
        self.assertEqual(self.events[0][1], "mobile_transfer.file")
        payload = self.events[0][2]
        self.assertEqual(payload["name"], "phone.png")
        self.assertEqual(base64.b64decode(payload["data_url"].split(",", 1)[1]), png)


class MobilePageTests(unittest.TestCase):
    def test_page_moves_fragment_token_to_session_storage(self):
        page = mobile_transfer._mobile_page("nonce")
        self.assertIn("location.hash", page)
        self.assertIn("sessionStorage.setItem", page)
        self.assertIn("history.replaceState", page)
        self.assertIn("never sent automatically", page)
        self.assertIn("Send to Unfold", page)
        self.assertIn("发送到 Unfold", page)
        self.assertNotIn("Hermes Browser", page)
        self.assertNotIn("https://", page)

    def test_lan_selection_rejects_clash_tun_fake_ip(self):
        selected = mobile_transfer.select_lan_address([
            ("198.18.0.1", 0),
            ("192.168.2.31", 1),
        ])
        self.assertEqual(selected, "192.168.2.31")

    def test_lan_selection_fails_instead_of_showing_an_unreachable_fake_ip(self):
        with self.assertRaisesRegex(RuntimeError, "No reachable local network"):
            mobile_transfer.select_lan_address([("198.18.0.1", 0), ("127.0.0.1", 1)])


if __name__ == "__main__":
    unittest.main()
