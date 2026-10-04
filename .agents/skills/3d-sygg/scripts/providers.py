#!/usr/bin/env python3
"""Provider, credential, ledger, and temporary publishing adapters for 3D sygg."""

from __future__ import annotations

import hashlib
import fcntl
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import http.client
import ipaddress
import json
import os
import pty
import re
import select
import shutil
import subprocess
import tempfile
import threading
import uuid
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


OMNI_BASE_URL = "https://api.wxart.space"
CANGYUAN_BASE_URL = "https://ai.cangyuansuanli.cn"
MINIMAX_BASE_URL = "https://api.minimaxi.com"
# Canonical model requested for both providers. wxart currently exposes this
# capability in its live catalog under the provider alias `omni-flash`.
VIDEO_MODEL_ID = "omni-fast-no-water"
OMNI_MODEL = "omni-flash"
CANGYUAN_MODEL = VIDEO_MODEL_ID
OMNI_ASPECT_RATIOS = {"9:16", "16:9"}
MINIMAX_MODEL = "speech-2.8-hd"
OMNI_KEYCHAIN_SERVICE = "3d-sygg-omni"
CANGYUAN_KEYCHAIN_SERVICE = "3d-sygg-cangyuan"
MINIMAX_KEYCHAIN_SERVICE = "3d-sygg-minimax"
KEYCHAIN_ACCOUNT = "api-key"
USER_AGENT = "3d-sygg/1.0"
VIDEO_PROVIDERS = {"omni", "cangyuan"}

# Production resolves credentials without prompting. The final layer migrates
# values from the user's allow-listed local configuration into macOS Keychain.
CREDENTIAL_ENV_NAMES: dict[str, tuple[str, ...]] = {
    OMNI_KEYCHAIN_SERVICE: ("WXART_OMNI_API_KEY", "WXART_API_KEY", "OMNI_API_KEY"),
    CANGYUAN_KEYCHAIN_SERVICE: ("CANGYUAN_API_KEY", "CANGYUAN_OMNI_API_KEY"),
    MINIMAX_KEYCHAIN_SERVICE: ("MINIMAX_API_KEY",),
}
CREDENTIAL_SOURCE_PATH_ENV: dict[str, str] = {
    OMNI_KEYCHAIN_SERVICE: "SYGG_OMNI_KEY_SOURCE",
    CANGYUAN_KEYCHAIN_SERVICE: "SYGG_CANGYUAN_KEY_SOURCE",
    MINIMAX_KEYCHAIN_SERVICE: "SYGG_MINIMAX_KEY_SOURCE",
}
# Open-source defaults never search the author's private directories.
# Users may explicitly opt into their own source through SYGG_*_KEY_SOURCE.
CREDENTIAL_SOURCE_FILES: dict[str, tuple[Path, ...]] = {
    OMNI_KEYCHAIN_SERVICE: (),
    CANGYUAN_KEYCHAIN_SERVICE: (),
    MINIMAX_KEYCHAIN_SERVICE: (),
}


class SyggError(RuntimeError):
    """Base runtime error."""


class ValidationError(SyggError):
    """An input or frozen-contract validation failed."""


class ProviderError(SyggError):
    """A provider returned a known failure, optionally with an HTTP status."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class SubmissionUnknown(SyggError):
    """A paid POST may have reached the provider and must not be retried."""


class PaidRequestBlocked(SyggError):
    """A request with the same fingerprint was already attempted."""


@contextmanager
def file_lock(path: Path, *, blocking: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise PaidRequestBlocked("Another command is already working on this project") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_reference_image(path: Path) -> str:
    """Validate a reference by file signature and return a safe public suffix."""
    if not path.is_file():
        raise ValidationError(f"Reference image is missing: {path}")
    size = path.stat().st_size
    with path.open("rb") as handle:
        header = handle.read(32)
        if size >= 12:
            handle.seek(-12, os.SEEK_END)
            footer = handle.read(12)
        else:
            footer = b""
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        if (
            size < 45
            or header[12:16] != b"IHDR"
            or int.from_bytes(header[16:20], "big") <= 0
            or int.from_bytes(header[20:24], "big") <= 0
            or footer[4:8] != b"IEND"
        ):
            raise ValidationError("Reference PNG is truncated or structurally invalid")
        return ".png"
    if header.startswith(b"\xff\xd8\xff"):
        if size < 16 or not footer.endswith(b"\xff\xd9"):
            raise ValidationError("Reference JPEG is truncated or structurally invalid")
        return ".jpg"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        if size < 20 or int.from_bytes(header[4:8], "little") + 8 != size:
            raise ValidationError("Reference WebP is truncated or structurally invalid")
        return ".webp"
    raise ValidationError("Reference must be a non-empty PNG, JPEG, or WebP image")


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _redact(value: str) -> str:
    value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
    value = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
    value = re.sub(r"(?i)((?:api[_-]?key|token|secret|signature)\s*[=:]\s*)[^\s&,;]+", r"\1[REDACTED]", value)
    return value


def _redact_secret(value: str, secret: str) -> str:
    redacted = _redact(value)
    return redacted.replace(secret, "[REDACTED]") if secret else redacted


def _validate_api_secret(value: str, service: str) -> str:
    secret = value.strip().strip("`").replace(r"\_", "_")
    placeholders = {"your_api_key", "api_key", "replace_me", "changeme"}
    if (
        len(secret) < 20
        or secret.lower() in placeholders
        or re.fullmatch(r"[A-Za-z0-9._-]+", secret) is None
    ):
        raise ValidationError(f"Invalid API credential found for service={service}")
    return secret


def _extract_local_credential(path: Path, service: str) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValidationError(f"Credential source cannot be read: {path}") from exc

    for name in CREDENTIAL_ENV_NAMES.get(service, ()):
        match = re.search(
            rf"(?m)^\s*(?:export\s+)?{re.escape(name)}\s*=\s*([^\r\n#]+)",
            text,
        )
        if match:
            raw = match.group(1).strip().strip("\"'")
            return _validate_api_secret(raw, service)

    if service == OMNI_KEYCHAIN_SERVICE:
        patterns = (
            r"(?im)^\s*Authorization\s*:\s*Bearer\s+`?([A-Za-z0-9._\\-]{20,})`?",
            r"(?im)^\s*API\s*key\s*[：:]\s*`?([A-Za-z0-9._\\-]{20,})`?",
        )
        candidates: list[str] = []
        for pattern in patterns:
            candidates.extend(re.findall(pattern, text))
        normalized = {_validate_api_secret(item, service) for item in candidates}
        if len(normalized) > 1:
            raise ValidationError(f"Credential source contains multiple wxart keys: {path}")
        if normalized:
            return normalized.pop()
    return None


def _store_keychain_secret_with_tty(
    security: str,
    service: str,
    account: str,
    secret: str,
    *,
    timeout: float = 15,
) -> None:
    args = [security, "add-generic-password", "-a", account, "-s", service, "-U", "-w"]
    if secret in args:
        raise ValidationError("Refusing to place an API credential in process arguments")
    pid, master = pty.fork()
    if pid == 0:  # pragma: no cover - child is replaced by the macOS security tool
        os.execv(security, args)

    output = bytearray()
    scan = bytearray()
    prompt_markers = (
        b"password data for new item:",
        b"retype password for new item:",
    )
    answered: set[bytes] = set()
    status: int | None = None
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    chunk = b""
                output.extend(chunk)
                scan.extend(chunk)
                lowered = bytes(scan).lower()
                for marker in prompt_markers:
                    if marker in lowered and marker not in answered:
                        os.write(master, secret.encode("utf-8") + b"\n")
                        answered.add(marker)
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                break
        if status is None:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            raise ValidationError(f"Timed out while updating Keychain item {service}")
        exit_code = os.waitstatus_to_exitcode(status)
        if exit_code != 0:
            detail = _redact_secret(output.decode("utf-8", errors="replace"), secret).strip()
            raise ValidationError(f"Could not update Keychain item {service}: {detail or exit_code}")
        if not answered:
            raise ValidationError(f"Keychain did not request a password for service={service}")
    finally:
        os.close(master)


class Keychain:
    """Resolve API secrets automatically and cache migrated values in Keychain."""

    @staticmethod
    def _security_command() -> str | None:
        return shutil.which("security")

    @classmethod
    def _load_stored(cls, service: str, account: str) -> str | None:
        security = cls._security_command()
        if not security:
            return None
        result = subprocess.run(
            [security, "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return _validate_api_secret(result.stdout, service)

    @classmethod
    def store(cls, service: str, secret: str, account: str = KEYCHAIN_ACCOUNT) -> None:
        security = cls._security_command()
        if not security:
            raise ValidationError("macOS security command is unavailable; Keychain cannot be updated")
        normalized = _validate_api_secret(secret, service)
        _store_keychain_secret_with_tty(security, service, account, normalized)
        stored = cls._load_stored(service, account)
        if stored != normalized:
            raise ValidationError(f"Keychain write verification failed for service={service}")

    @classmethod
    def resolve(
        cls,
        service: str,
        account: str = KEYCHAIN_ACCOUNT,
        *,
        environ: Mapping[str, str] | None = None,
        source_paths: Iterable[Path] | None = None,
    ) -> tuple[str, str]:
        if service not in CREDENTIAL_ENV_NAMES:
            raise ValidationError(f"Unknown credential service: {service}")
        environment = os.environ if environ is None else environ
        for name in CREDENTIAL_ENV_NAMES[service]:
            value = environment.get(name)
            if value:
                secret = _validate_api_secret(value, service)
                return secret, f"environment:{name}"

        stored = cls._load_stored(service, account)
        if stored:
            return stored, "keychain"

        paths: list[Path] = []
        override_name = CREDENTIAL_SOURCE_PATH_ENV[service]
        override = environment.get(override_name, "").strip()
        if override:
            paths.extend(Path(item).expanduser() for item in override.split(os.pathsep) if item.strip())
        configured_sources = CREDENTIAL_SOURCE_FILES[service] if source_paths is None else source_paths
        paths.extend(Path(item).expanduser() for item in configured_sources)

        seen: set[Path] = set()
        for path in paths:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            secret = _extract_local_credential(resolved, service)
            if secret:
                cls.store(service, secret, account)
                return secret, f"local-migration:{resolved.name}"

        env_names = ", ".join(CREDENTIAL_ENV_NAMES[service])
        raise ValidationError(
            f"API credential is unavailable for service={service}; checked environment ({env_names}), "
            "macOS Keychain, and approved local credential sources. Production never prompts for keys."
        )

    @classmethod
    def load(cls, service: str, account: str = KEYCHAIN_ACCOUNT) -> str:
        secret, _source = cls.resolve(service, account)
        return secret

    @classmethod
    def bootstrap(cls) -> dict[str, dict[str, str | bool]]:
        result: dict[str, dict[str, str | bool]] = {}
        for label, service in (("omni", OMNI_KEYCHAIN_SERVICE), ("cangyuan", CANGYUAN_KEYCHAIN_SERVICE),
                               ("minimax", MINIMAX_KEYCHAIN_SERVICE)):
            secret, source = cls.resolve(service, KEYCHAIN_ACCOUNT)
            result[label] = {
                "service": service,
                "account": KEYCHAIN_ACCOUNT,
                "available": bool(secret),
                "source": source,
            }
        return result


class TaskLedger:
    """Append-only logical ledger backed by atomically replaced JSON."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"Task ledger cannot be read: {self.path}") from exc
        if not isinstance(data, list):
            raise ValidationError(f"Task ledger is not a JSON list: {self.path}")
        return data

    @staticmethod
    def _matches_attempt(row: dict[str, Any], attempt: dict[str, Any]) -> bool:
        if attempt.get("attempt_id"):
            return row.get("attempt_id") == attempt["attempt_id"]
        return row.get("provider") == attempt.get("provider") and row.get("request_hash") == attempt.get("request_hash")

    @classmethod
    def _retry_parent(cls, records: list[dict[str, Any]], clip_index: int) -> str | None:
        attempts = [r for r in records if r.get("provider") in VIDEO_PROVIDERS and r.get("event") == "attempted"
                    and r.get("clip_index") == clip_index]
        budget_attempts = [r for r in attempts if not r.get("fallback_of")]
        if not attempts or len(budget_attempts) >= 1 + budget_attempts[0].get("max_retries", 0):
            return None
        latest = attempts[-1]
        related = [r for r in records if cls._matches_attempt(r, latest)]
        submitted = next((r for r in reversed(related) if r.get("event") == "submitted"), None)
        if submitted:
            terminal = next((r for r in reversed(records) if r.get("task_id") == submitted["task_id"]
                             and r.get("event") in {"failed", "completed", "downloaded"}), None)
            if terminal and terminal.get("event") == "failed" and terminal.get("status") in {"failed", "error"}:
                return str(submitted["task_id"])
        rejected = next((r for r in reversed(related) if r.get("event") == "rejected"), None)
        if rejected and rejected.get("http_status") == 429:
            return latest.get("attempt_id")
        return None

    def retry_parent(self, clip_index: int) -> str | None:
        return self._retry_parent(self.records(), clip_index)

    def append(self, **record: Any) -> dict[str, Any]:
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}
        with self._lock, file_lock(self.path.with_suffix(".lock")):
            records = self.records()
            if record.get("event") == "attempted":
                try:
                    from .capabilities import check_budget
                except ImportError:
                    from capabilities import check_budget
                check_budget(records, record, getattr(self, "budget", None))
                for old in records:
                    if old.get("event") == "attempted" and not any(
                        self._matches_attempt(r, old) and r.get("event") in {"submitted", "completed", "rejected"}
                        for r in records
                    ):
                        raise PaidRequestBlocked("An earlier paid attempt has no confirmed result")
                    if old.get("event") == "submission_unknown":
                        raise PaidRequestBlocked("An earlier paid submission is unresolved")
                previous = [r for r in records if r.get("event") == "attempted"
                            and r.get("provider") == record.get("provider")
                            and ((record.get("provider") == "minimax" and r.get("operation_id") == record.get("operation_id")) or (record.get("provider") != "minimax" and r.get("clip_index") == record.get("clip_index")))]
                if previous:
                    # A deterministic primary rejection may hand the same approved
                    # operation to the configured fallback provider. It is linked
                    # to the primary attempt but does not consume retry quota.
                    if record.get("fallback_of"):
                        parent = next((r for r in records if r.get("event") == "attempted"
                                       and r.get("attempt_id") == record.get("fallback_of")), None)
                        if not parent or parent.get("clip_index") != record.get("clip_index"):
                            raise PaidRequestBlocked("Fallback must preserve the approved clip binding")
                        records.append(entry)
                        atomic_write_json(self.path, records)
                        return entry
                    parent = self._retry_parent(records, record.get("clip_index"))
                    if record.get("provider") not in VIDEO_PROVIDERS or not parent or record.get("retry_of") != parent:
                        raise PaidRequestBlocked("This logical paid operation was already attempted; no eligible retry remains")
                    baseline = next((old for old in reversed(previous) if old.get("provider") == record.get("provider")), previous[0])
                    if (not record.get("semantic_hash") or record["semantic_hash"] != baseline.get("semantic_hash")
                            or record.get("max_retries") != previous[0].get("max_retries")):
                        raise PaidRequestBlocked("Retry must preserve the approved request, references and retry limit")
                elif record.get("retry_of"):
                    raise PaidRequestBlocked("Retry has no original attempt")
            records.append(entry)
            atomic_write_json(self.path, records)
        return entry

    def already_attempted(self, provider: str, request_hash: str) -> bool:
        return any(
            row.get("provider") == provider
            and row.get("request_hash") == request_hash
            and row.get("event") == "attempted"
            for row in self.records()
        )

    def submitted_tasks(self) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for row in self.records():
            task_id = row.get("task_id")
            if task_id:
                latest[str(task_id)] = row
        return list(latest.values())


class JsonHttpClient:
    """Small urllib client. Paid callers decide whether a POST is retryable."""

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float = 60,
        ambiguous_on_transport: bool = False,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        if body is not None:
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            detail = _redact(raw.decode("utf-8", errors="replace"))[:500]
            if ambiguous_on_transport and (exc.code == 408 or exc.code >= 500):
                raise SubmissionUnknown(
                    f"HTTP {exc.code} after a paid POST: {detail or exc.reason}"
                ) from exc
            raise ProviderError(f"HTTP {exc.code}: {detail or exc.reason}", status_code=exc.code) from exc
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
            message = f"{type(exc).__name__}: {_redact(str(exc))}"
            if ambiguous_on_transport:
                raise SubmissionUnknown(message) from exc
            raise ProviderError(message) from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if ambiguous_on_transport:
                raise SubmissionUnknown("provider returned invalid JSON after a paid POST") from exc
            raise ProviderError("provider returned invalid JSON") from exc
        if not isinstance(data, dict):
            if ambiguous_on_transport:
                raise SubmissionUnknown("provider returned a non-object JSON result after a paid POST")
            raise ProviderError("provider returned a non-object JSON result")
        return data

    def safe_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any] | None = None,
        attempts: int = 3,
        timeout: float = 30,
    ) -> dict[str, Any]:
        last: Exception | None = None
        for index in range(attempts):
            try:
                return self.request_json(method, url, headers=headers, payload=payload, timeout=timeout)
            except ProviderError as exc:
                last = exc
                if index + 1 < attempts:
                    time.sleep(min(2**index, 4))
        raise ProviderError(f"safe query failed after {attempts} attempts: {last}")

    def safe_download(
        self,
        url: str,
        output: Path,
        *,
        attempts: int = 3,
        timeout: float = 90,
        headers: dict[str, str] | None = None,
    ) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        last: Exception | None = None
        for index in range(attempts):
            temp = output.with_suffix(output.suffix + ".part")
            try:
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
                with urllib.request.urlopen(request, timeout=timeout) as response, temp.open("wb") as handle:
                    shutil.copyfileobj(response, handle)
                if temp.stat().st_size <= 0:
                    raise ProviderError("downloaded file is empty")
                os.replace(temp, output)
                return output
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ProviderError) as exc:
                last = exc
                temp.unlink(missing_ok=True)
                if index + 1 < attempts:
                    time.sleep(min(2**index, 4))
        raise ProviderError(f"download failed after {attempts} attempts: {_redact(str(last))}")


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _nested(data: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = data
        for key in path:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        if current not in (None, ""):
            return current
    return None


class OmniClient:
    """Recorded Omni attempts with bounded, same-reference terminal-failure recovery."""

    provider_name = "omni"
    request_prefix = "omni"
    model = OMNI_MODEL

    def __init__(
        self,
        api_key: str,
        ledger: TaskLedger,
        *,
        base_url: str = OMNI_BASE_URL,
        http: JsonHttpClient | None = None,
    ):
        self.api_key = api_key
        self.ledger = ledger
        self.base_url = base_url.rstrip("/")
        self.http = http or JsonHttpClient()

    @staticmethod
    def build_payload(
        prompt: str,
        image_urls: list[str],
        *,
        aspect_ratio: str,
    ) -> dict[str, Any]:
        if not prompt.strip():
            raise ValidationError("Omni prompt must not be empty")
        if not isinstance(aspect_ratio, str) or aspect_ratio not in OMNI_ASPECT_RATIOS:
            raise ValidationError("Omni aspect_ratio must be 9:16 or 16:9")
        if not 1 <= len(image_urls) <= 3:
            raise ValidationError("Omni ref mode requires 1 to 3 reference images")
        for url in image_urls:
            validate_public_https_url(url)
        return {
            "model": OMNI_MODEL,
            "prompt": prompt.strip(),
            "mode": "ref",
            "images_url": image_urls,
            "aspect_ratio": aspect_ratio,
            "duration": 10,
            "resolution": "720p",
            "watermark": False,
        }

    def submit(
        self,
        prompt: str,
        image_urls: list[str],
        *,
        clip_index: int,
        aspect_ratio: str,
        reference_bindings: list[dict[str, Any]] | None = None,
        max_retries: int = 0,
        retry_of: str | None = None,
        fallback_of: str | None = None,
    ) -> str:
        if type(max_retries) is not int or not 0 <= max_retries <= 3:
            raise ValidationError("max_retries must be an integer between 0 and 3")
        if max_retries and reference_bindings is None:
            raise ValidationError("Automatic video retries require immutable reference bindings")
        payload = self.build_payload(prompt, image_urls, aspect_ratio=aspect_ratio)
        if reference_bindings is not None:
            if len(reference_bindings) != len(image_urls):
                raise ValidationError("Omni reference binding count must match images_url")
            for position, (binding, url) in enumerate(zip(reference_bindings, image_urls, strict=True), start=1):
                if binding.get("position") != position or binding.get("url") != url:
                    raise ValidationError("Omni reference bindings must match images_url order exactly")
                if not str(binding.get("role") or "") or not str(binding.get("sha256") or ""):
                    raise ValidationError("Omni reference bindings require role and sha256")
        request_hash = canonical_hash({"clip_index": clip_index, "payload": payload})
        semantic_hash = canonical_hash({"clip_index": clip_index,
            "payload": {k: v for k, v in payload.items() if k not in ({"images_url", "images", "reference_image_urls", "first_image_url", "last_image_url", "reference_videos", "reference_audios"} if getattr(self.ledger, "schema_version", 8) >= 9 else {"images_url"})},
            "references": [{k: v for k, v in r.items() if k != "url"} for r in (reference_bindings or [])]})
        attempt_id = uuid.uuid4().hex
        request_record = self.ledger.path.parent / f"{self.request_prefix}-clip-{clip_index:02d}-{attempt_id}.request.json"
        record: dict[str, Any] = {
            "provider": self.provider_name,
            "clip_index": clip_index,
            "request_hash": request_hash,
            "payload": payload,
            "attempt_id": attempt_id, "semantic_hash": semantic_hash,
            "retry_of": retry_of, "fallback_of": fallback_of, "max_retries": max_retries,
        }
        if reference_bindings is not None:
            record["reference_bindings"] = reference_bindings
        atomic_write_json(request_record, record)
        self.ledger.append(
            provider=self.provider_name, event="attempted", clip_index=clip_index,
            request_hash=request_hash, request_record=str(request_record),
            attempt_id=attempt_id, semantic_hash=semantic_hash, retry_of=retry_of, max_retries=max_retries,
            fallback_of=fallback_of,
        )
        try:
            data = self.http.request_json(
                "POST",
                f"{self.base_url}/v1/videos",
                headers=_headers(self.api_key),
                payload=payload,
                timeout=90,
                ambiguous_on_transport=True,
            )
        except SubmissionUnknown as exc:
            safe_error = _redact_secret(str(exc), self.api_key)
            self.ledger.append(
                provider=self.provider_name, event="submission_unknown", clip_index=clip_index,
                request_hash=request_hash, attempt_id=attempt_id, error=safe_error,
            )
            raise SubmissionUnknown(safe_error) from exc
        except ProviderError as exc:
            safe_error = _redact_secret(str(exc), self.api_key)
            self.ledger.append(
                provider=self.provider_name, event="rejected", clip_index=clip_index,
                request_hash=request_hash, attempt_id=attempt_id, error=safe_error, http_status=exc.status_code,
            )
            raise ProviderError(safe_error, status_code=exc.status_code) from exc
        task_id = _nested(data, ("task_id",), ("id",), ("data", "task_id"), ("data", "id"))
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
            self.ledger.append(
                provider=self.provider_name, event="submission_unknown", clip_index=clip_index,
                request_hash=request_hash, attempt_id=attempt_id, error="missing task_id",
            )
            raise SubmissionUnknown(f"{self.provider_name} paid POST returned no task_id; do not resubmit")
        task_id = str(task_id)
        self.ledger.append(
            provider=self.provider_name, event="submitted", clip_index=clip_index,
            request_hash=request_hash, task_id=task_id, attempt_id=attempt_id, retry_of=retry_of,
        )
        return task_id

    def check_available(self) -> None:
        """Run a non-billable model-list check before the first paid POST."""
        try:
            data = self.http.safe_json(
                "GET", f"{self.base_url}/v1/models", headers=_headers(self.api_key), attempts=2, timeout=20
            )
        except ProviderError as exc:
            raise ProviderError(f"{self.provider_name} health check failed: {exc}", status_code=exc.status_code) from exc
        rows = data.get("data") if isinstance(data, dict) else None
        if isinstance(rows, list) and rows:
            model_ids = {str(row.get("id")) for row in rows if isinstance(row, dict)}
            if self.model not in model_ids:
                raise ProviderError(f"{self.provider_name} model {self.model} is not listed", status_code=404)

    def poll(self, task_id: str, *, timeout_seconds: int = 3600, poll_interval: float = 5.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        url = f"{self.base_url}/v1/videos/{urllib.parse.quote(task_id, safe='')}"
        while True:
            try:
                data = self.http.safe_json("GET", url, headers=_headers(self.api_key), attempts=3, timeout=30)
            except ProviderError as exc:
                self.ledger.append(provider=self.provider_name, event="query_error", task_id=task_id,
                                   error=_redact_secret(str(exc), self.api_key))
                if time.monotonic() >= deadline:
                    raise ProviderError(f"{self.provider_name} task {task_id} query interrupted; use resume, never resubmit") from exc
                time.sleep(poll_interval)
                continue
            raw_status = _redact_secret(str(_nested(data, ("status",), ("data", "status")) or ""), self.api_key).lower()
            progress = _nested(data, ("progress",), ("data", "progress"))
            upstream_error = _nested(data, ("error",), ("data", "error"))
            error = None
            if upstream_error is not None:
                if isinstance(upstream_error, dict):
                    error = {key: _redact_secret(str(upstream_error[key]), self.api_key)[:2000]
                             for key in ("code", "message") if key in upstream_error}
                else:
                    error = {"message": _redact_secret(str(upstream_error), self.api_key)[:2000]}
            observation = {"provider": self.provider_name, "task_id": task_id, "status": raw_status,
                           "progress": progress if isinstance(progress, (int, float)) else None, "error": error}
            if raw_status in {"completed", "complete", "success", "succeeded", "done"}:
                self.ledger.append(event="completed", **observation)
                return data
            if raw_status in {"failed", "error", "cancelled", "canceled"}:
                self.ledger.append(event="failed", cause="undetermined", **observation)
                detail = json.dumps(error, ensure_ascii=False) if error else "no upstream detail"
                raise ProviderError(f"{self.provider_name} task {task_id} failed; cause undetermined; {detail}")
            self.ledger.append(event="observed", **observation)
            if time.monotonic() >= deadline:
                self.ledger.append(provider=self.provider_name, event="poll_timeout", task_id=task_id)
                raise ProviderError(f"{self.provider_name} task {task_id} is still pending; use resume instead of resubmitting")
            time.sleep(poll_interval)

    def download_result(self, task_id: str, data: dict[str, Any], output: Path) -> Path:
        url = _nested(
            data,
            ("video_url",), ("output_url",), ("url",),
            ("data", "video_url"), ("data", "output_url"), ("data", "url"),
        )
        if not isinstance(url, str):
            raise ProviderError(f"Omni task {task_id} completed without a downloadable video URL")
        try:
            validate_public_https_url(url)
        except ValidationError as exc:
            raise ProviderError(f"Omni task {task_id} returned an unsafe video URL: {exc}") from exc
        try:
            result = self.http.safe_download(url, output)
        except ProviderError as exc:
            raise ProviderError(_redact_secret(str(exc), self.api_key)) from exc
        self.ledger.append(
            provider=self.provider_name, event="downloaded", task_id=task_id,
            path=str(result), sha256=sha256_file(result),
        )
        return result


class CangyuanClient(OmniClient):
    """OpenAI-compatible Omni fallback at ai.cangyuansuanli.cn."""

    provider_name = "cangyuan"
    request_prefix = "cangyuan"
    model = CANGYUAN_MODEL

    def __init__(self, api_key: str, ledger: TaskLedger, *, base_url: str = CANGYUAN_BASE_URL,
                 http: JsonHttpClient | None = None):
        super().__init__(api_key, ledger, base_url=base_url, http=http)

    @staticmethod
    def build_payload(prompt: str, image_urls: list[str], *, aspect_ratio: str) -> dict[str, Any]:
        if not prompt.strip():
            raise ValidationError("Cangyuan prompt must not be empty")
        if not isinstance(aspect_ratio, str) or aspect_ratio not in OMNI_ASPECT_RATIOS:
            raise ValidationError("Cangyuan aspect_ratio must be 9:16 or 16:9")
        if not 1 <= len(image_urls) <= 5:
            raise ValidationError("Cangyuan image-to-video requires 1 to 5 reference images")
        for url in image_urls:
            validate_public_https_url(url)
        payload: dict[str, Any] = {
            "model": CANGYUAN_MODEL,
            "prompt": prompt.strip(),
            # This relay treats a lone first_image_url as a first/last-frame
            # request and rejects it. Its `images` field is the single-image
            # image-to-video route and also supports multiple references.
            "images": image_urls,
            "seconds": "10",
            "aspect_ratio": aspect_ratio,
            "resolution": "720p",
        }
        return payload

    def download_result(self, task_id: str, data: dict[str, Any], output: Path) -> Path:
        url = _nested(data, ("video_url",), ("output_url",), ("url",),
                      ("data", "video_url"), ("data", "output_url"), ("data", "url"))
        if isinstance(url, str) and url:
            try:
                validate_public_https_url(url)
            except ValidationError as exc:
                raise ProviderError(f"Cangyuan task {task_id} returned an unsafe video URL: {exc}") from exc
            try:
                result = self.http.safe_download(url, output, headers=_headers(self.api_key) if urllib.parse.urlsplit(url).netloc == urllib.parse.urlsplit(self.base_url).netloc else None)
            except ProviderError:
                # The relay may expose metadata.url for inspection while
                # requiring authenticated /content for the actual download.
                result = None
        else:
            result = None
        if result is None:
            # Some OpenAI-compatible relays expose the completed MP4 only through /content.
            content_url = f"{self.base_url}/v1/videos/{urllib.parse.quote(task_id, safe='')}/content"
            try:
                result = self.http.safe_download(content_url, output, headers=_headers(self.api_key))
            except ProviderError as exc:
                raise ProviderError(_redact_secret(str(exc), self.api_key)) from exc
        self.ledger.append(provider=self.provider_name, event="downloaded", task_id=task_id,
                           path=str(result), sha256=sha256_file(result))
        return result


class MiniMaxClient:
    """MiniMax Speech 2.8 HD adapter restricted to system voices."""

    def __init__(
        self,
        api_key: str,
        ledger: TaskLedger,
        *,
        base_url: str | None = None,
        http: JsonHttpClient | None = None,
    ):
        self.api_key = api_key
        self.ledger = ledger
        self.base_url = (base_url or os.environ.get("MINIMAX_BASE_URL") or MINIMAX_BASE_URL).rstrip("/")
        self.http = http or JsonHttpClient()

    def list_system_voices(self) -> list[dict[str, Any]]:
        try:
            data = self.http.safe_json(
                "POST", f"{self.base_url}/v1/get_voice", headers=_headers(self.api_key),
                payload={"voice_type": "all"}, attempts=3, timeout=45,
            )
        except ProviderError as exc:
            raise ProviderError(_redact_secret(str(exc), self.api_key)) from exc
        try:
            _raise_minimax_error(data)
        except ProviderError as exc:
            raise ProviderError(_redact_secret(str(exc), self.api_key)) from exc
        voices = data.get("system_voice", [])
        if not isinstance(voices, list):
            raise ProviderError("MiniMax get_voice returned invalid system_voice")
        return [row for row in voices if isinstance(row, dict) and row.get("voice_id")]

    @staticmethod
    def build_payload(text: str, voice_id: str, *, speed: float = 1.0) -> dict[str, Any]:
        if not text.strip():
            raise ValidationError("MiniMax narration text must not be empty")
        if not voice_id.strip():
            raise ValidationError("MiniMax system voice_id must not be empty")
        if not 0.5 <= speed <= 2.0:
            raise ValidationError("MiniMax voice speed must be between 0.5 and 2.0")
        return {
            "model": MINIMAX_MODEL,
            "text": text.strip(),
            "stream": False,
            "voice_setting": {"voice_id": voice_id, "speed": speed, "vol": 1.0, "pitch": 0},
            "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1},
            "subtitle_enable": False,
            "output_format": "hex",
        }

    def synthesize(
        self,
        text: str,
        voice_id: str,
        output: Path,
        *,
        verified_system_voices: Iterable[dict[str, Any]],
        speed: float = 1.0,
        operation_id: str | None = None,
    ) -> Path:
        allowed = {str(row.get("voice_id")) for row in verified_system_voices if row.get("voice_id")}
        if voice_id not in allowed:
            raise ValidationError("Selected MiniMax voice_id is not in the current system_voice list")
        payload = self.build_payload(text, voice_id, speed=speed)
        request_hash = canonical_hash({"operation_id": operation_id, "payload": payload}) if operation_id else canonical_hash(payload)
        if self.ledger.already_attempted("minimax", request_hash):
            raise PaidRequestBlocked("This MiniMax narration request was already attempted")
        request_record = self.ledger.path.parent / f"minimax-narration-{request_hash[:12]}.request.json"
        atomic_write_json(
            request_record,
            {"provider": "minimax", "request_hash": request_hash, "payload": payload},
        )
        self.ledger.append(
            provider="minimax", event="attempted", request_hash=request_hash,
            voice_id=voice_id, request_record=str(request_record), operation_id=operation_id,
        )
        try:
            data = self.http.request_json(
                "POST", f"{self.base_url}/v1/t2a_v2", headers=_headers(self.api_key),
                payload=payload, timeout=180, ambiguous_on_transport=True,
            )
            _raise_minimax_error(data)
        except SubmissionUnknown as exc:
            safe_error = _redact_secret(str(exc), self.api_key)
            self.ledger.append(
                provider="minimax", event="submission_unknown", request_hash=request_hash,
                error=safe_error,
            )
            raise SubmissionUnknown(safe_error) from exc
        except ProviderError as exc:
            safe_error = _redact_secret(str(exc), self.api_key)
            self.ledger.append(
                provider="minimax", event="rejected", request_hash=request_hash,
                error=safe_error,
            )
            raise ProviderError(safe_error) from exc
        audio_hex = _nested(data, ("data", "audio"))
        if not isinstance(audio_hex, str) or not audio_hex:
            self.ledger.append(
                provider="minimax", event="submission_unknown", request_hash=request_hash,
                error="missing audio payload",
            )
            raise SubmissionUnknown("MiniMax paid POST returned no audio; do not resubmit")
        try:
            audio = bytes.fromhex(audio_hex)
        except ValueError as exc:
            self.ledger.append(
                provider="minimax", event="submission_unknown", request_hash=request_hash,
                error="invalid audio hex",
            )
            raise SubmissionUnknown("MiniMax returned invalid audio after a paid POST") from exc
        if not audio:
            self.ledger.append(
                provider="minimax", event="submission_unknown", request_hash=request_hash,
                error="empty audio payload",
            )
            raise SubmissionUnknown("MiniMax returned empty audio after a paid POST")
        output.parent.mkdir(parents=True, exist_ok=True)
        temp = output.with_suffix(output.suffix + ".part")
        try:
            with temp.open("wb") as handle:
                handle.write(audio)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, output)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            safe_error = _redact_secret(f"MiniMax audio was received but local save failed: {exc}", self.api_key)
            self.ledger.append(
                provider="minimax", event="local_write_failed", request_hash=request_hash,
                error=safe_error,
            )
            raise ProviderError(safe_error) from exc
        self.ledger.append(
            provider="minimax", event="completed", request_hash=request_hash,
            path=str(output), sha256=sha256_file(output), trace_id=data.get("trace_id"),
        )
        return output


def _raise_minimax_error(data: dict[str, Any]) -> None:
    base = data.get("base_resp")
    if isinstance(base, dict) and base.get("status_code") not in (None, 0, "0"):
        raise ProviderError(f"MiniMax error {base.get('status_code')}: {_redact(str(base.get('status_msg') or 'unknown'))}")


VOICE_KEYWORDS: dict[str, list[str]] = {
    "tech": ["沉稳高管", "播报男", "可靠", "低沉", "reliable executive", "male", "professional"],
    "luxury": ["沉稳高管", "成熟", "专业女", "新闻女", "reliable executive", "mature", "female"],
    "lifestyle": ["温柔学姐", "温暖闺蜜", "柔和", "亲切女", "gentle", "warm", "female"],
    "youth": ["清脆少女", "不羁青年", "活力", "年轻", "crisp", "young", "energetic"],
}


def choose_system_voice(category: str, voices: list[dict[str, Any]]) -> dict[str, Any]:
    if category not in VOICE_KEYWORDS:
        raise ValidationError(f"Unknown voice category: {category}")
    if not voices:
        raise ValidationError("No MiniMax system voices are available")
    keywords = VOICE_KEYWORDS[category]
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for order, voice in enumerate(voices):
        descriptions = voice.get("description") or []
        if isinstance(descriptions, str):
            descriptions = [descriptions]
        haystack = " ".join(
            [str(voice.get("voice_name") or ""), str(voice.get("voice_id") or "")]
            + [str(item) for item in descriptions]
        ).lower()
        score = sum((len(keywords) - index) for index, keyword in enumerate(keywords) if keyword.lower() in haystack)
        scored.append((score, -order, voice))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    if scored[0][0] <= 0:
        raise ValidationError(f"No system_voice matches the {category} routing rules; manual selection is required")
    return scored[0][2]


def validate_public_https_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValidationError("Reference URL must be a public HTTPS URL without embedded credentials")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".local"):
        raise ValidationError("Reference URL must not use localhost or a local domain")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValidationError("Reference URL must not use a private or non-global IP address")
    return url


@dataclass(frozen=True)
class PublishedAsset:
    source: str
    url: str
    sha256: str


class TemporaryPublisher:
    """One allow-listed publication session, kept alive throughout known-task polling.

    Logs are outside the public directory. Access proves an HTTP response, not
    that the video provider fetched or interpreted the image successfully.
    """
    def __init__(self, approved: list[dict[str, str]], *, startup_timeout: float = 90,
                 evidence_dir: Path | None = None):
        self.approved = approved
        self.startup_timeout = startup_timeout
        self.evidence_dir = evidence_dir or Path(tempfile.mkdtemp(prefix="3d-sygg-evidence-"))
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.events = TaskLedger(self.evidence_dir / f"session-{time.time_ns()}.json")
        self.temp = None
        self.http_server = None
        self.http_thread = None
        self.http_process = None  # Legacy inspection compatibility; HTTP now runs in-process.
        self.tunnel_process = None
        self.log_handle = None
        self.log_path = None
        self.public_origin = None
        self._staged = {}
        self._stop = threading.Event()
        self._monitor = None
        self._public_available = None

    def __enter__(self):
        try:
            self.start()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def stage(self):
        if self.temp is None:
            self.temp = tempfile.TemporaryDirectory(prefix="3d-sygg-publish-")
        staged = {}
        for row in self.approved:
            source = Path(row["path"]).expanduser().resolve()
            suffix = validate_reference_image(source)
            digest = sha256_file(source)
            if digest != row["sha256"]:
                raise ValidationError(f"Approved reference changed after confirmation: {source}")
            target = Path(self.temp.name) / f"{digest}{suffix}"
            if not target.exists():
                shutil.copy2(source, target)
            if sha256_file(target) != digest:
                raise ValidationError("Reference changed while staging")
            staged[str(source)] = (target, digest)
        self._staged = staged
        return staged

    def _start_http(self):
        publisher = self
        allowed = {"/" + target.name for target, _ in self._staged.values()}
        root = self.temp.name
        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=root, **kwargs)

            def send_head(self):
                if self.path not in allowed:
                    self.send_error(404)
                    return None
                return super().send_head()

            def log_message(self, format, *args):
                pass

            def log_request(self, code="-", size="-"):
                publisher.events.append(event="http_access", method=self.command,
                    asset=self.path if self.path in allowed else "unlisted",
                    status=code, observer="local_probe" if self.headers.get("User-Agent") == USER_AGENT else "unattributed",
                    supplier_fetch_confirmed=False)
        self.http_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
        self.http_thread.start()
        return self.http_server.server_port

    def start(self):
        if not self.stage():
            raise ValidationError("No approved reference files were provided")
        cloudflared = shutil.which("cloudflared")
        if not cloudflared:
            raise ValidationError("cloudflared is required for temporary HTTPS publishing")
        port = self._start_http()
        self.log_path = self.evidence_dir / f"cloudflared-{time.time_ns()}.log"
        self.log_handle = self.log_path.open("w", encoding="utf-8")
        self.tunnel_process = subprocess.Popen(
            [cloudflared, "tunnel", "--protocol", "http2", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
            stdout=self.log_handle, stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self.tunnel_process.poll() is not None:
                break
            content = self.log_path.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", content)
            if match and "Registered tunnel connection" in content:
                self.public_origin = match.group(0)
                self.events.append(event="ready", origin=self.public_origin,
                    hashes=[digest for _, digest in self._staged.values()], supplier_fetch_confirmed=False)
                return
            time.sleep(0.25)
        raise ProviderError("Cloudflare Quick Tunnel did not become ready; see retained publication evidence")

    def health_check(self, *, require_public: bool = True):
        healthy = bool(self.tunnel_process and self.tunnel_process.poll() is None
                       and self.http_thread and self.http_thread.is_alive())
        self.events.append(event="health", processes_alive=healthy, supplier_fetch_confirmed=False)
        if not healthy or (require_public and self._public_available is False):
            raise ProviderError("Temporary reference publisher is unavailable; do not start another paid task")

    def _observe(self):
        tick = 0
        while not self._stop.wait(10):
            tick += 1
            try:
                self.health_check(require_public=False)
                if tick % 3:
                    continue
                for asset in self._published:
                    if self._stop.is_set():
                        return
                    verify_public_asset(asset.url, asset.sha256, attempts=1, timeout=5)
                self._public_available = True
                self.events.append(event="public_probe", verified=True, supplier_fetch_confirmed=False)
            except (ProviderError, ValidationError) as exc:
                self._public_available = False
                self.events.append(event="public_probe", verified=False, error=_redact(str(exc)), supplier_fetch_confirmed=False)

    def publish(self):
        if not self.public_origin:
            raise ValidationError("Temporary publisher has not started")
        results = []
        for source, (target, digest) in self._staged.items():
            url = f"{self.public_origin}/{target.name}"
            verify_public_asset(url, digest)
            self.events.append(event="public_verified", url=url, sha256=digest, supplier_fetch_confirmed=False)
            results.append(PublishedAsset(source=source, url=url, sha256=digest))
        self._published = results
        self._public_available = True
        if self._monitor is None:
            self._monitor = threading.Thread(target=self._observe, daemon=True)
            self._monitor.start()
        return results

    def close(self):
        self._stop.set()
        if self._monitor:
            self._monitor.join(timeout=20)
        if self.tunnel_process and self.tunnel_process.poll() is None:
            self.tunnel_process.terminate()
            try:
                self.tunnel_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.tunnel_process.kill()
                self.tunnel_process.wait(timeout=2)
        if self.http_server:
            self.http_server.shutdown()
            self.http_server.server_close()
        if self.http_thread:
            self.http_thread.join(timeout=2)
        if self.log_handle:
            self.log_handle.close()
        if self.temp:
            self.temp.cleanup()
        self.events.append(event="closed", origin=self.public_origin,
            reason="session ended; Quick Tunnel URL cannot be recreated on resume", supplier_fetch_confirmed=False)
        self.temp = self.tunnel_process = self.http_server = self.http_process = None
        self.log_handle = None


def verify_public_asset(
    url: str,
    expected_sha256: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    attempts: int = 4,
    timeout: float = 20,
) -> None:
    validate_public_https_url(url)
    last: Exception | None = None
    for index in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with opener(request, timeout=timeout) as response:
                digest = hashlib.sha256(response.read()).hexdigest()
            if digest != expected_sha256:
                raise ValidationError("Published reference SHA-256 does not match the approved local file")
            return
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            OSError,
            ValidationError,
        ) as exc:
            last = exc
            if index + 1 < attempts:
                time.sleep(0.5 * (index + 1))
    raise ProviderError(f"Temporary HTTPS asset verification failed: {_redact(str(last))}")
