import base64
import hashlib
import io
from contextlib import redirect_stdout
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from qiling_images import KEY_SERVICE, authorize, run_job, secure_store, settings
from image_jobs import read
from image_jobs import pack_references, write
from image_collection import finalize_collection


class QilingTests(unittest.TestCase):
    def setUp(self):
        try:
            import httpx
            from openai import OpenAI
            from PIL import Image
        except ImportError:
            self.skipTest("OpenAI, httpx and Pillow required for mock HTTP tests")
        self.temp = tempfile.TemporaryDirectory()
        self.topic = Path(self.temp.name).resolve()
        self.config = {"api_url": "https://platform.invalid", "model": "doubao-seedream-5-0-flash"}
        output = io.BytesIO()
        Image.new("RGB", (12, 16), "red").save(output, format="PNG")
        self.png = output.getvalue()
        self.requests = []
        self.httpx, self.OpenAI = httpx, OpenAI
        self.client = self.make_client(self.success)
        self.job = dict(id="C01", title="Hero", stage=2, mode="text_to_image", style_id="2d-chibi-comic",
                        topic=str(self.topic), output_dir="images/candidates/assets", prompt="draw a mascot",
                        references=[], reference_images=[], job_key="test-image-job")

    def tearDown(self):
        if hasattr(self, "client"):
            self.client.close()
            self.temp.cleanup()

    def make_client(self, handler):
        return self.OpenAI(api_key="test-secret-key", base_url=self.config["api_url"] + "/v1", max_retries=0,
                           http_client=self.httpx.Client(transport=self.httpx.MockTransport(handler)))

    def success(self, request):
        self.requests.append(request)
        return self.httpx.Response(200, json=dict(created=1, data=[dict(b64_json=base64.b64encode(self.png).decode())],
                                                qiling=dict(id="call-123", billing=dict(fee="0.01"))))

    def test_browser_result_uses_configured_origin_and_only_secure_storage(self):
        backend, auth = Mock(), Mock()
        auth.authorize.return_value = dict(status="authorized", api_key="a-secret", base_url="https://other.invalid")
        result = authorize(self.config, store=backend, authorization=auth)
        backend.set_password.assert_called_once_with(KEY_SERVICE, self.config["api_url"], "a-secret")
        self.assertNotIn("a-secret", json.dumps(result))

    def test_official_sdk_authorization_flow_with_mocked_server_and_browser(self):
        try:
            from qiling_ai_sdk import QilingAuthorizationClient
        except ImportError:
            self.skipTest("Install the optional official Qiling SDK to test its authorization flow")
        auth = QilingAuthorizationClient(self.config["api_url"], client_name="brain-comic")
        auth._http._request = Mock(side_effect=[
            dict(device_code="private-device-code", verification_url="https://platform.invalid/approve",
                 expires_in=600, interval=3), dict(status="pending"), dict(status="authorized", api_key="private-key")])
        backend, stream = Mock(), io.StringIO()
        with patch("qiling_ai_sdk.authorization.webbrowser.open", return_value=True) as browser, \
                patch("qiling_ai_sdk.authorization.time.sleep") as sleep, redirect_stdout(stream):
            result = authorize(self.config, store=backend, authorization=auth)
        self.assertTrue(result["authorized"])
        browser.assert_called_once_with("https://platform.invalid/approve")
        sleep.assert_called_once_with(3)
        self.assertNotIn("private-key", stream.getvalue())
        self.assertNotIn("private-device-code", stream.getvalue())
        self.assertEqual(auth._http._request.call_args_list[0].args[1], "/api/v1/auth/api-key/requests")

    def test_denied_authorization_never_saves_a_key(self):
        backend, auth = Mock(), Mock()
        auth.authorize.return_value = dict(status="denied")
        with self.assertRaises(ValueError):
            authorize(self.config, store=backend, authorization=auth)
        backend.set_password.assert_not_called()

    def test_plaintext_keyring_is_refused(self):
        class Insecure:
            pass
        Insecure.__module__ = "keyrings.alt.file"
        keyring = Mock()
        keyring.get_keyring.return_value = Insecure()
        with patch.dict("sys.modules", {"keyring": keyring}):
            with self.assertRaises(ValueError):
                secure_store()

    def test_uses_requested_model_and_no_duplicate_submit(self):
        first = run_job(self.job, self.config, self.client)
        second = run_job(self.job, self.config, self.client)
        self.assertEqual(first, second)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.url.path, "/v1/images/generations")
        self.assertEqual(request.headers["authorization"], "Bearer test-secret-key")
        body = json.loads(request.content)
        self.assertEqual(body["model"], self.config["model"])
        self.assertEqual(body["n"], 1)
        self.assertEqual(request.headers["Idempotency-Key"], first["request_id"])
        for path in self.topic.rglob("*.json"):
            self.assertNotIn("test-secret-key", path.read_text())
        asset = read(self.topic / "images/manifest.json")["assets"][0]
        self.assertEqual(asset["provider"]["call_id"], "call-123")
        self.assertEqual(asset["review"], "pending")

    def test_multipart_upload_uses_actual_images_in_slot_order(self):
        other = self.png + b"other-bytes"
        paths = [self.topic / "first.png", self.topic / "second.png"]
        for path, data in zip(paths, [self.png, other]):
            path.write_bytes(data)
        job = {**self.job, "id": "shot-01", "stage": 3, "mode": "image_to_image", "output_dir": "images/candidates/comic",
               "references": [dict(asset_id=f"C{i:02}", image_index=i, path=str(p), sha256=hashlib.sha256(p.read_bytes()).hexdigest())
                              for i, p in enumerate(paths, 1)], "reference_images": [str(p) for p in paths]}
        run_job(job, self.config, self.client)
        req = self.requests[0]
        self.assertEqual(req.url.path, "/v1/images/edits")
        self.assertIn("multipart/form-data", req.headers["content-type"])
        self.assertLess(req.content.index(b'filename="first.png"'), req.content.index(b'filename="second.png"'))
        self.assertIn(other, req.content)

    def test_changed_reference_stops_before_any_api_call(self):
        path = self.topic / "image.png"
        path.write_bytes(self.png)
        job = {**self.job, "references": [dict(path=str(path), sha256="wrong")], "reference_images": [str(path)]}
        with self.assertRaises(ValueError):
            run_job(job, self.config, self.client)
        self.assertFalse(self.requests)

    def test_output_outside_topic_stops_before_generation(self):
        with self.assertRaises(ValueError):
            run_job({**self.job, "output_dir": "../outside"}, self.config, self.client)
        self.assertFalse(self.requests)

    def test_timeout_is_not_retried_or_resent_by_a_second_run(self):
        def timeout(request):
            self.requests.append(request)
            raise self.httpx.ReadTimeout("timeout", request=request)
        client = self.make_client(timeout)
        with client:
            with self.assertRaises(Exception):
                run_job(self.job, self.config, client)
            with self.assertRaisesRegex(ValueError, "原任务"):
                run_job(self.job, self.config, client)
            with self.assertRaisesRegex(ValueError, "原任务"):
                run_job(self.job, self.config, client, new_attempt=True)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(read(next((self.topic / ".qiling-jobs").glob("*.json")))["status"], "unknown")

    def test_received_result_can_resume_saving_without_regeneration(self):
        with patch("qiling_images.image_bytes", side_effect=ValueError("local failure")):
            with self.assertRaises(ValueError):
                run_job(self.job, self.config, self.client)
        result = run_job(self.job, self.config, self.client)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["status"], "saved")

    def test_new_image_needs_explicit_new_attempt(self):
        first = run_job(self.job, self.config, self.client)
        second = run_job(self.job, self.config, self.client, new_attempt=True)
        self.assertEqual(len(self.requests), 2)
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertNotEqual(first["file"], second["file"])

    def test_local_journal_failure_does_not_create_a_second_image_copy(self):
        original_write = write
        def fail_saved(path, data):
            if data.get("status") == "saved":
                raise OSError("disk failure")
            original_write(path, data)
        with patch("qiling_images.write", side_effect=fail_saved):
            with self.assertRaises(OSError):
                run_job(self.job, self.config, self.client)
        result = run_job(self.job, self.config, self.client)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["status"], "saved")
        self.assertEqual(len(list((self.topic / "images/candidates/assets").glob("*.png"))), 1)

    def test_saved_image_can_be_found_after_collection_cleanup(self):
        first = run_job(self.job, self.config, self.client)
        manifest = self.topic / "images/manifest.json"
        data = read(manifest)
        data["assets"][0]["review"] = "passed"
        write(manifest, data)
        finalize_collection(self.topic, 2)
        second = run_job(self.job, self.config, self.client)
        self.assertNotEqual(first["file"], second["file"])
        self.assertEqual(len(self.requests), 1)
        self.assertTrue((self.topic / second["file"]).exists())

    def test_reference_packs_upload_physical_input_and_keep_logical_bindings(self):
        paths = [self.topic / "ref1.png", self.topic / "ref2.png"]
        for path in paths:
            path.write_bytes(self.png)
        refs = [dict(asset_id=f"C{i:02}", image_index=i, path=str(p), sha256=hashlib.sha256(p.read_bytes()).hexdigest())
                for i, p in enumerate(paths, 1)]
        job = pack_references({**self.job, "id": "shot-01", "stage": 3,
                               "output_dir": "images/candidates/comic", "prompt": "图1和图2", "mode": "image_to_image",
                               "references": refs, "reference_images": [str(p) for p in paths]}, 1)
        run_job(job, self.config, self.client)
        body = self.requests[0].content
        self.assertEqual(body.count(b'filename="'), 1)
        saved = read(self.topic / "images/comic-manifest.json")["shots"][0]
        self.assertEqual(len(saved["references"]), 2)
        self.assertEqual(len(saved["input_images"]), 1)

    def test_base_url_normalizes_v1_once_and_rejects_credential_urls(self):
        with patch.dict("os.environ", {"QILING_API_URL": "https://platform.invalid/v1/"}):
            self.assertEqual(settings()["api_url"], "https://platform.invalid")
        with patch.dict("os.environ", {"QILING_API_URL": "https://name:secret@platform.invalid"}):
            with self.assertRaises(ValueError):
                settings()


if __name__ == "__main__":
    unittest.main()
