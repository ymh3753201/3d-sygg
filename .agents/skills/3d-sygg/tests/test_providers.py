from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import setup_keys  # noqa: E402
import providers  # noqa: E402
from providers import (  # noqa: E402
    CANGYUAN_MODEL,
    CangyuanClient,
    Keychain,
    MINIMAX_KEYCHAIN_SERVICE,
    MiniMaxClient,
    OMNI_KEYCHAIN_SERVICE,
    JsonHttpClient,
    OmniClient,
    PaidRequestBlocked,
    ProviderError,
    SubmissionUnknown,
    TaskLedger,
    TemporaryPublisher,
    ValidationError,
    choose_system_voice,
    verify_public_asset,
)


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeHttp:
    def __init__(self, *, post_result=None, post_error=None):
        self.post_result = post_result
        self.post_error = post_error
        self.calls = []

    def request_json(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.post_error:
            raise self.post_error
        return self.post_result


class ProviderContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = TaskLedger(self.root / "ledger.json")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_omni_payload_has_exact_whitelist_and_fixed_values(self) -> None:
        payload = OmniClient.build_payload(
            "prompt", ["https://assets.example.com/a.png"], aspect_ratio="9:16"
        )
        self.assertEqual(
            set(payload),
            {"model", "prompt", "mode", "images_url", "aspect_ratio", "duration", "resolution", "watermark"},
        )
        self.assertEqual(payload["model"], "omni-flash")
        self.assertEqual(payload["mode"], "ref")
        self.assertEqual(payload["duration"], 10)
        self.assertEqual(payload["resolution"], "720p")
        self.assertEqual(payload["aspect_ratio"], "9:16")
        self.assertFalse(payload["watermark"])

    def test_cangyuan_payload_uses_single_image_video_route(self) -> None:
        payload = CangyuanClient.build_payload(
            "prompt", ["https://assets.example.com/a.png"], aspect_ratio="9:16"
        )
        self.assertEqual(payload["model"], CANGYUAN_MODEL)
        self.assertEqual(payload["images"], ["https://assets.example.com/a.png"])
        self.assertEqual(payload["seconds"], "10")
        self.assertEqual(payload["resolution"], "720p")
        self.assertNotIn("first_image_url", payload)

        horizontal = OmniClient.build_payload(
            "prompt", ["https://assets.example.com/a.png"], aspect_ratio="16:9"
        )
        self.assertEqual(horizontal["aspect_ratio"], "16:9")

        with self.assertRaisesRegex(ValidationError, "9:16 or 16:9"):
            OmniClient.build_payload(
                "prompt", ["https://assets.example.com/a.png"], aspect_ratio="1:1"
            )

    def test_omni_rejects_reference_count_and_non_public_url(self) -> None:
        with self.assertRaises(ValidationError):
            OmniClient.build_payload("prompt", [], aspect_ratio="9:16")
        with self.assertRaises(ValidationError):
            OmniClient.build_payload(
                "prompt",
                ["https://a.example/x", "https://b.example/x", "https://c.example/x", "https://d.example/x"],
                aspect_ratio="9:16",
            )
        with self.assertRaises(ValidationError):
            OmniClient.build_payload("prompt", ["http://127.0.0.1/a.png"], aspect_ratio="9:16")

    def test_ambiguous_paid_post_is_written_once_and_never_retried(self) -> None:
        http = FakeHttp(post_error=SubmissionUnknown("timeout"))
        client = OmniClient("secret", self.ledger, http=http)
        with self.assertRaises(SubmissionUnknown):
            client.submit(
                "prompt", ["https://assets.example.com/a.png"], clip_index=1, aspect_ratio="9:16"
            )
        self.assertEqual(len(http.calls), 1)
        events = [row["event"] for row in self.ledger.records()]
        self.assertEqual(events, ["attempted", "submission_unknown"])
        request_record = Path(self.ledger.records()[0]["request_record"])
        self.assertTrue(request_record.is_file())
        recorded = json.loads(request_record.read_text(encoding="utf-8"))
        self.assertEqual(recorded["payload"]["duration"], 10)
        self.assertEqual(recorded["payload"]["resolution"], "720p")
        self.assertNotIn("secret", request_record.read_text(encoding="utf-8"))
        with self.assertRaises(PaidRequestBlocked):
            client.submit(
                "prompt", ["https://assets.example.com/a.png"], clip_index=1, aspect_ratio="9:16"
            )
        self.assertEqual(len(http.calls), 1)

    def test_omni_request_record_freezes_reference_role_hash_and_order(self) -> None:
        http = FakeHttp(post_result={"id": "task-1", "status": "queued"})
        client = OmniClient("secret", self.ledger, http=http)
        url = "https://assets.example.com/shot-01.png"
        binding = {
            "position": 1,
            "role": "storyboard_reference",
            "clip_index": 1,
            "shot_index": 1,
            "sha256": "a" * 64,
            "url": url,
        }
        self.assertEqual(
            client.submit(
                "prompt", [url], clip_index=1, aspect_ratio="16:9", reference_bindings=[binding]
            ),
            "task-1",
        )
        request_record = Path(self.ledger.records()[0]["request_record"])
        recorded = json.loads(request_record.read_text(encoding="utf-8"))
        self.assertEqual(recorded["reference_bindings"], [binding])
        self.assertEqual(recorded["payload"]["images_url"], [url])
        self.assertEqual(recorded["payload"]["aspect_ratio"], "16:9")

    def test_omni_reference_binding_must_match_url_order(self) -> None:
        client = OmniClient("secret", self.ledger, http=FakeHttp(post_result={"id": "task-1"}))
        with self.assertRaisesRegex(ValidationError, "match images_url order"):
            client.submit(
                "prompt",
                ["https://assets.example.com/a.png"],
                clip_index=1,
                aspect_ratio="9:16",
                reference_bindings=[
                    {
                        "position": 1,
                        "role": "storyboard_reference",
                        "sha256": "b" * 64,
                        "url": "https://assets.example.com/b.png",
                    }
                ],
            )
        self.assertEqual(client.http.calls, [])
        self.assertEqual(self.ledger.records(), [])

    def test_missing_task_id_is_submission_unknown(self) -> None:
        http = FakeHttp(post_result={"status": "queued"})
        client = OmniClient("secret", self.ledger, http=http)
        with self.assertRaises(SubmissionUnknown):
            client.submit(
                "prompt", ["https://assets.example.com/a.png"], clip_index=1, aspect_ratio="9:16"
            )
        self.assertEqual(self.ledger.records()[-1]["event"], "submission_unknown")

    def test_paid_post_http_5xx_is_treated_as_unknown(self) -> None:
        error = urllib.error.HTTPError(
            "https://provider.example/v1/jobs", 502, "bad gateway", {}, io.BytesIO(b"upstream failed")
        )
        self.addCleanup(error.close)
        with mock.patch("providers.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(SubmissionUnknown):
                JsonHttpClient().request_json(
                    "POST", "https://provider.example/v1/jobs", payload={"x": 1},
                    ambiguous_on_transport=True,
                )

    def test_provider_error_redacts_nonstandard_api_key(self) -> None:
        secret = "provider-token-without-sk-prefix"
        http = FakeHttp(post_error=ProviderError(f"provider echoed {secret}"))
        client = OmniClient(secret, self.ledger, http=http)
        with self.assertRaises(ProviderError) as caught:
            client.submit(
                "prompt", ["https://assets.example.com/a.png"], clip_index=1, aspect_ratio="9:16"
            )
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, json.dumps(self.ledger.records()))

    def test_omni_download_rejects_non_public_or_non_https_url(self) -> None:
        client = OmniClient("secret", self.ledger, http=FakeHttp())
        for url in ("http://assets.example.com/video.mp4", "https://127.0.0.1/video.mp4"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ProviderError, "unsafe video URL"):
                    client.download_result("task-1", {"video_url": url}, self.root / "video.mp4")

    def test_minimax_rejects_voice_outside_verified_system_list_before_post(self) -> None:
        http = FakeHttp(post_result={})
        client = MiniMaxClient("secret", self.ledger, http=http)
        with self.assertRaisesRegex(ValidationError, "not in the current system_voice"):
            client.synthesize(
                "广告旁白", "cloned-private-voice", self.root / "voice.mp3",
                verified_system_voices=[{"voice_id": "system-voice"}],
            )
        self.assertEqual(http.calls, [])

    def test_minimax_ambiguous_paid_post_is_not_retried(self) -> None:
        http = FakeHttp(post_error=SubmissionUnknown("timeout"))
        client = MiniMaxClient("secret", self.ledger, http=http)
        voices = [{"voice_id": "system-voice"}]
        with self.assertRaises(SubmissionUnknown):
            client.synthesize(
                "广告旁白", "system-voice", self.root / "voice.mp3",
                verified_system_voices=voices,
            )
        self.assertEqual(len(http.calls), 1)
        with self.assertRaises(PaidRequestBlocked):
            client.synthesize(
                "广告旁白", "system-voice", self.root / "voice.mp3",
                verified_system_voices=voices,
            )
        self.assertEqual(len(http.calls), 1)

    def test_minimax_local_save_failure_is_logged_without_a_second_post(self) -> None:
        http = FakeHttp(post_result={"data": {"audio": "494433"}, "base_resp": {"status_code": 0}})
        client = MiniMaxClient("secret", self.ledger, http=http)
        voices = [{"voice_id": "system-voice"}]
        output = self.root / "voice.mp3"
        real_replace = os.replace

        def fail_audio_replace(source, destination):
            if Path(destination) == output:
                raise OSError("disk full")
            return real_replace(source, destination)

        with mock.patch("providers.os.replace", side_effect=fail_audio_replace):
            with self.assertRaisesRegex(ProviderError, "local save failed"):
                client.synthesize(
                    "广告旁白", "system-voice", output,
                    verified_system_voices=voices,
                )
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(self.ledger.records()[-1]["event"], "local_write_failed")
        with self.assertRaises(PaidRequestBlocked):
            client.synthesize(
                "广告旁白", "system-voice", self.root / "voice.mp3",
                verified_system_voices=voices,
            )
        self.assertEqual(len(http.calls), 1)

    def test_voice_routing_uses_only_supplied_system_voices(self) -> None:
        voices = [
            {"voice_id": "v1", "voice_name": "温暖闺蜜", "description": ["柔和亲切"]},
            {"voice_id": "v2", "voice_name": "沉稳高管", "description": ["可靠低沉"]},
        ]
        self.assertEqual(choose_system_voice("tech", voices)["voice_id"], "v2")
        self.assertEqual(choose_system_voice("lifestyle", voices)["voice_id"], "v1")

    def test_temporary_publisher_stages_only_approved_files(self) -> None:
        approved = self.root / "approved.png"
        unapproved = self.root / "private.png"
        approved.write_bytes(PNG_BYTES)
        unapproved.write_bytes(b"private")
        digest = hashlib.sha256(approved.read_bytes()).hexdigest()
        publisher = TemporaryPublisher([{"path": str(approved), "sha256": digest}])
        try:
            staged = publisher.stage()
            served = list(Path(publisher.temp.name).iterdir())
            self.assertEqual(len(staged), 1)
            self.assertEqual(len(served), 1)
            self.assertEqual(served[0].read_bytes(), PNG_BYTES)
            self.assertNotIn("private", served[0].name)
        finally:
            publisher.close()

    def test_public_hash_verification(self) -> None:
        payload = b"same-bytes"
        digest = hashlib.sha256(payload).hexdigest()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return payload

        verify_public_asset("https://assets.example.com/ref.png", digest, opener=lambda *a, **k: Response())
        with self.assertRaises(Exception):
            verify_public_asset("https://assets.example.com/ref.png", "0" * 64, opener=lambda *a, **k: Response(), attempts=1)

    def test_public_hash_verification_retries_incomplete_read(self) -> None:
        payload = b"same-bytes"
        digest = hashlib.sha256(payload).hexdigest()
        calls = 0

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise http.client.IncompleteRead(b"partial", 4)
                return payload

        verify_public_asset(
            "https://assets.example.com/ref.png",
            digest,
            opener=lambda *a, **k: Response(),
        )
        self.assertEqual(calls, 2)

    def test_keychain_store_does_not_put_secret_in_command_arguments(self) -> None:
        secret = "super-secret-value-123456789"
        with mock.patch.object(Keychain, "_security_command", return_value="/usr/bin/security"), mock.patch.object(
            providers, "_store_keychain_secret_with_tty"
        ) as secure_store, mock.patch.object(
            Keychain, "_load_stored", return_value=secret
        ), mock.patch.object(providers.subprocess, "run") as run:
            Keychain.store(OMNI_KEYCHAIN_SERVICE, secret)
        secure_store.assert_called_once_with("/usr/bin/security", OMNI_KEYCHAIN_SERVICE, "api-key", secret)
        run.assert_not_called()

    def test_keychain_store_requires_successful_readback(self) -> None:
        secret = "super-secret-value-123456789"
        with mock.patch.object(Keychain, "_security_command", return_value="/usr/bin/security"), mock.patch.object(
            providers, "_store_keychain_secret_with_tty"
        ), mock.patch.object(Keychain, "_load_stored", return_value="different-secret-value-123456789"):
            with self.assertRaisesRegex(ValidationError, "write verification failed"):
                Keychain.store(OMNI_KEYCHAIN_SERVICE, secret)

    def test_keychain_resolve_auto_migrates_wxart_markdown_without_prompt(self) -> None:
        secret = "wxart-secret-12345678901234567890"
        source = self.root / "omni.md"
        source.write_text(f"Authorization: Bearer {secret}\nAPI key：{secret}\n", encoding="utf-8")
        with mock.patch.object(Keychain, "_load_stored", return_value=None), mock.patch.object(
            Keychain, "_security_command", return_value="/usr/bin/security"
        ), mock.patch.object(Keychain, "store") as store:
            value, origin = Keychain.resolve(
                OMNI_KEYCHAIN_SERVICE,
                environ={},
                source_paths=[source],
            )
        self.assertEqual(value, secret)
        self.assertEqual(origin, "local-migration:omni.md")
        store.assert_called_once_with(OMNI_KEYCHAIN_SERVICE, secret, "api-key")

    def test_keychain_resolve_auto_migrates_minimax_dotenv_without_prompt(self) -> None:
        secret = "minimax-secret-12345678901234567890"
        source = self.root / ".env"
        source.write_text(f"MINIMAX_API_KEY={secret}\n", encoding="utf-8")
        with mock.patch.object(Keychain, "_load_stored", return_value=None), mock.patch.object(
            Keychain, "_security_command", return_value="/usr/bin/security"
        ), mock.patch.object(Keychain, "store") as store:
            value, origin = Keychain.resolve(
                MINIMAX_KEYCHAIN_SERVICE,
                environ={},
                source_paths=[source],
            )
        self.assertEqual(value, secret)
        self.assertEqual(origin, "local-migration:.env")
        store.assert_called_once_with(MINIMAX_KEYCHAIN_SERVICE, secret, "api-key")

    def test_environment_override_is_automatic_without_keychain_mutation(self) -> None:
        secret = "minimax-env-secret-12345678901234567890"
        with mock.patch.object(Keychain, "_load_stored", return_value=None), mock.patch.object(
            Keychain, "_security_command", return_value="/usr/bin/security"
        ), mock.patch.object(Keychain, "store") as store:
            value, origin = Keychain.resolve(
                MINIMAX_KEYCHAIN_SERVICE,
                environ={"MINIMAX_API_KEY": secret},
                source_paths=[],
            )
        self.assertEqual(value, secret)
        self.assertEqual(origin, "environment:MINIMAX_API_KEY")
        store.assert_not_called()

    def test_default_setup_is_noninteractive(self) -> None:
        status = {
            "omni": {"available": True, "source": "keychain", "service": OMNI_KEYCHAIN_SERVICE},
            "minimax": {"available": True, "source": "keychain", "service": MINIMAX_KEYCHAIN_SERVICE},
        }
        with mock.patch.object(setup_keys.Keychain, "bootstrap", return_value=status), mock.patch.object(
            setup_keys.getpass, "getpass"
        ) as getpass_call:
            self.assertEqual(setup_keys.main([]), 0)
        getpass_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
