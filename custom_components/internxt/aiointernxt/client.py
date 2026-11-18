"""Async client for Internxt Drive.

Protocol ported from internxt/rclone-adapter (MIT). Uses the CLI login endpoint,
which Internxt offers for third-party clients on Ultimate and Business plans.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
import functools
import hashlib
import json as jsonlib
import logging
import math
import random
import re
import secrets
import time
from typing import Any

import aiohttp

from . import crypto
from .exceptions import (
    InternxtAccountBlocked,
    InternxtAuthError,
    InternxtConflictError,
    InternxtConnectionError,
    InternxtError,
    InternxtIntegrityError,
    InternxtNotFoundError,
    InternxtPlanError,
    InternxtQuotaError,
    InternxtRateLimitError,
    InternxtStreamError,
    InternxtTwoFactorRequired,
)
from .models import (
    Credentials,
    DriveFile,
    DriveFolder,
    SecurityDetails,
    User,
    split_name,
)

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://gateway.internxt.com"
DEFAULT_CLIENT_NAME = "aiointernxt"

MIB = 1024 * 1024
MULTIPART_THRESHOLD = (
    100 * MIB
)  # Internxt requires multipart from 100 MiB on (adapter, rclone)
DEFAULT_PART_SIZE = (
    16 * MIB
)  # adapter: 30 MiB x 6 in parallel; we go sequential to save RAM
MIN_PART_SIZE = 5 * MIB  # S3 minimum
MAX_PARTS = 10_000  # S3 maximum
CRYPTO_BLOCK = 1 * MIB  # plaintext handed to the cipher at once on the single-PUT path
THREAD_THRESHOLD = 256 * 1024  # crypto on larger blocks runs in a thread
RETRIES = 3
MAX_RETRY_WAIT = 60.0
FOLDER_SETTLE_DELAY = 0.5  # adapter waits 500 ms before using a new folder
LIMITS_TTL = 3600.0

ProgressCallback = Callable[[int], None]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _translate_unexpected[**P, R](
    func: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """Turn surprises in response shapes into InternxtError, so callers only see our errors."""

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await func(*args, **kwargs)
        except InternxtError:
            raise
        except (KeyError, TypeError, AttributeError, IndexError, ValueError) as err:
            raise InternxtError(
                f"unexpected response: {type(err).__name__}: {err}"
            ) from err

    return wrapper


def _retry_wait(attempt: int, err: InternxtConnectionError) -> float:
    if isinstance(err, InternxtRateLimitError) and err.retry_after is not None:
        return min(err.retry_after, MAX_RETRY_WAIT)
    return min(float(2**attempt) + random.random(), MAX_RETRY_WAIT)  # noqa: S311


class InternxtClient:
    """Internxt Drive client.

    Pass an aiohttp session owned by the caller. After `login()` or
    `set_credentials()` + `refresh()`, read `credentials` and persist them:
    the token changes on every refresh.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        client_name: str = DEFAULT_CLIENT_NAME,
        base_url: str = BASE_URL,
        request_timeout: float = 60,
    ) -> None:
        self._session = session
        self._client_name = client_name
        self._drive = f"{base_url}/drive"
        self._network = f"{base_url}/network"
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)
        self._slow_timeout = aiohttp.ClientTimeout(total=max(request_timeout, 300))
        # No total limit for transfers: a restore over a slow line may take hours.
        self._transfer_timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=30, sock_read=300
        )
        self._credentials: Credentials | None = None
        self._user: User | None = None
        self._bridge_auth: str | None = None
        self._refresh_lock = asyncio.Lock()
        self._bucket_keys: dict[tuple[str, str], bytes] = {}
        self._limits: tuple[float, int | None] | None = None

    # ------------------------------------------------------------------ state

    @property
    def credentials(self) -> Credentials:
        if self._credentials is None:
            raise InternxtAuthError("not logged in")
        return self._credentials

    @property
    def user(self) -> User:
        if self._user is None:
            raise InternxtError("no account data yet, call refresh() first")
        return self._user

    @property
    def token_expires_at(self) -> datetime | None:
        """Expiry from the JWT's exp claim (not verified), None if unknown."""
        if self._credentials is None:
            return None
        try:
            payload = self._credentials.token.split(".")[1]
            claims = jsonlib.loads(
                base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            )
            return datetime.fromtimestamp(int(claims["exp"]), UTC)
        except (IndexError, ValueError, KeyError, TypeError):
            return None

    def set_credentials(self, credentials: Credentials) -> None:
        self._credentials = credentials

    def _set_user(self, user: User) -> None:
        self._user = user
        self._bridge_auth = crypto.bridge_auth_header(user.bridge_user, user.user_id)

    # ------------------------------------------------------------------ http

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"internxt-client": self._client_name}
        if extra:
            headers.update(extra)
        return headers

    def _bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credentials.token}"}

    def _basic(self) -> dict[str, str]:
        self.user  # noqa: B018 - raises if refresh() was never called
        assert self._bridge_auth is not None
        return {"Authorization": self._bridge_auth, "internxt-version": "1.0"}

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        params: dict[str, str] | None = None,
        expect_json: bool = True,
        timeout: aiohttp.ClientTimeout | None = None,  # noqa: ASYNC109 - per-request aiohttp timeout
    ) -> Any:
        attempts = RETRIES if method == "GET" else 1
        for attempt in range(1, attempts + 1):
            try:
                return await self._request_once(
                    method, url, headers=headers, json=json, params=params,
                    expect_json=expect_json, timeout=timeout or self._timeout,
                )  # fmt: skip
            except InternxtConnectionError as err:
                if attempt == attempts:
                    raise
                wait = _retry_wait(attempt, err)
                _LOGGER.debug(
                    "%s %s failed (%s), retry in %.1fs", method, url, err, wait
                )
                await asyncio.sleep(wait)
        raise AssertionError("unreachable")

    async def _request_once(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None,
        json: Any,
        params: dict[str, str] | None,
        expect_json: bool,
        timeout: aiohttp.ClientTimeout,  # noqa: ASYNC109
    ) -> Any:
        what = f"{method} {url}"
        try:
            async with self._session.request(
                method, url, headers=self._headers(headers), json=json, params=params,
                timeout=timeout,
            ) as resp:  # fmt: skip
                if resp.status >= 400:
                    await _raise_for_status(resp, what)
                if not expect_json or resp.status == 204:
                    return None
                text = await resp.text()
        except TimeoutError as err:
            raise InternxtConnectionError(f"timeout on {what}") from err
        except aiohttp.ClientError as err:
            raise InternxtConnectionError(
                f"{what}: {type(err).__name__}: {err}"
            ) from err
        try:
            return jsonlib.loads(text) if text else None
        except ValueError as err:
            raise InternxtError(f"{what}: response is not JSON") from err

    # ------------------------------------------------------------------ auth

    @_translate_unexpected
    async def security_details(self, email: str) -> SecurityDetails:
        """First login step: whether 2FA is on, plus the encrypted salt."""
        data = await self._request(
            "POST", f"{self._drive}/auth/login", json={"email": email}
        )
        return SecurityDetails(
            two_factor=bool(data.get("tfa")), encrypted_salt=data["sKey"]
        )

    @_translate_unexpected
    async def login(
        self, email: str, password: str, tfa_code: str | None = None
    ) -> Credentials:
        """Log in and decrypt the mnemonic. The password is not kept.

        Every failed attempt (wrong password or wrong 2FA code) counts toward
        Internxt's lock after 10 failures, so never retry automatically.
        """
        details = await self.security_details(email)
        if details.two_factor and not tfa_code:
            raise InternxtTwoFactorRequired("2FA code required")
        try:
            encrypted = await asyncio.to_thread(
                crypto.encrypt_password_hash, password, details.encrypted_salt
            )
        except ValueError as err:
            raise InternxtError("could not process the login salt") from err
        body: dict[str, Any] = {"email": email, "password": encrypted}
        if tfa_code:
            body["tfa"] = tfa_code
        data = await self._request(
            "POST", f"{self._drive}/auth/cli/login/access", json=body
        )
        try:
            mnemonic = crypto.decrypt_text(data["user"]["mnemonic"], password)
        except (ValueError, UnicodeDecodeError) as err:
            raise InternxtAuthError("could not decrypt the account key") from err
        if len(mnemonic.split()) not in (12, 15, 18, 21, 24):
            raise InternxtAuthError("decrypted account key is not a valid mnemonic")
        self._credentials = Credentials(token=data["newToken"], mnemonic=mnemonic)
        self._set_user(User.from_api(data["user"]))
        return self._credentials

    @_translate_unexpected
    async def refresh(self) -> User:
        """Get a new token and load account data. Persist `credentials` afterwards.

        Concurrent calls share one refresh.
        """
        token_before = self.credentials.token
        async with self._refresh_lock:
            if self._user is not None and self.credentials.token != token_before:
                return self._user  # someone else refreshed while we waited
            data = await self._request(
                "GET", f"{self._drive}/users/cli/refresh", headers=self._bearer()
            )
            token = data.get("newToken")
            if not token:
                raise InternxtError("refresh response missing newToken")
            self._credentials = Credentials(
                token=token, mnemonic=self.credentials.mnemonic
            )
            self._set_user(User.from_api(data["user"]))
            return self._user  # type: ignore[return-value]

    # ------------------------------------------------------------------ account

    @_translate_unexpected
    async def usage(self) -> dict[str, Any]:
        data = await self._request(
            "GET", f"{self._drive}/users/usage", headers=self._bearer()
        )
        return dict(data)

    @_translate_unexpected
    async def limit(self) -> dict[str, Any]:
        data = await self._request(
            "GET", f"{self._drive}/users/limit", headers=self._bearer()
        )
        return dict(data)

    @_translate_unexpected
    async def max_upload_size(self, *, cached: bool = True) -> int | None:
        """Largest file the plan allows, cached for an hour like the adapter does."""
        if cached and self._limits and time.monotonic() - self._limits[0] < LIMITS_TTL:
            return self._limits[1]
        data = await self._request(
            "GET", f"{self._drive}/files/limits", headers=self._bearer()
        )
        value = data.get("maxUploadFileSize") if isinstance(data, dict) else None
        result = int(value) if value else None
        self._limits = (time.monotonic(), result)
        return result

    # ------------------------------------------------------------------ folders

    async def _list(self, folder_uuid: str, kind: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str | None = None
        while True:
            params = {"limit": "1000", "order": "ASC"}
            if cursor:
                params["cursor"] = cursor
            data = await self._request(
                "GET",
                f"{self._drive}/folders/v2/content/{folder_uuid}/{kind}",
                headers=self._bearer(),
                params=params,
            )
            items.extend(data.get(kind) or [])
            cursor = data.get("nextCursor")
            if not cursor or cursor in seen:
                return items
            seen.add(cursor)

    @_translate_unexpected
    async def list_files(self, folder_uuid: str) -> list[DriveFile]:
        return [DriveFile.from_api(f) for f in await self._list(folder_uuid, "files")]

    @_translate_unexpected
    async def list_folders(self, folder_uuid: str) -> list[DriveFolder]:
        return [
            DriveFolder.from_api(f) for f in await self._list(folder_uuid, "folders")
        ]

    @_translate_unexpected
    async def create_folder(self, parent_uuid: str, name: str) -> DriveFolder:
        now = _now_iso()
        data = await self._request(
            "POST",
            f"{self._drive}/folders",
            headers=self._bearer(),
            json={
                "plainName": name,
                "parentFolderUuid": parent_uuid,
                "creationTime": now,
                "modificationTime": now,
            },
        )
        await asyncio.sleep(FOLDER_SETTLE_DELAY)
        return DriveFolder.from_api(data)

    @_translate_unexpected
    async def ensure_folder(self, parent_uuid: str, name: str) -> DriveFolder:
        for folder in await self.list_folders(parent_uuid):
            if folder.name == name:
                return folder
        return await self.create_folder(parent_uuid, name)

    # ------------------------------------------------------------------ files

    @_translate_unexpected
    async def delete_file(self, file_uuid: str) -> None:
        """Delete permanently (not to trash)."""
        await self._request(
            "DELETE",
            f"{self._drive}/files/{file_uuid}",
            headers=self._bearer(),
            expect_json=False,
        )

    async def _file_key_iv(self, index: bytes) -> tuple[bytes, bytes]:
        mnemonic, bucket = self.credentials.mnemonic, self.user.bucket
        bucket_key = self._bucket_keys.get((mnemonic, bucket))
        if bucket_key is None:
            bucket_key = await asyncio.to_thread(crypto.bucket_key, mnemonic, bucket)
            self._bucket_keys = {(mnemonic, bucket): bucket_key}
        return crypto.file_key_iv_from_bucket_key(bucket_key, index)

    @_translate_unexpected
    async def upload(
        self,
        stream: AsyncIterator[bytes],
        *,
        size: int,
        folder_uuid: str,
        name: str,
        progress: ProgressCallback | None = None,
        part_size: int = DEFAULT_PART_SIZE,
    ) -> DriveFile:
        """Encrypt and upload a stream of exactly `size` bytes.

        Errors from `stream` itself are raised as InternxtStreamError.
        """
        plain_name, file_type = split_name(name)
        max_size = await self._max_upload_size_or_none()
        if max_size is not None and size > max_size:
            raise InternxtQuotaError(
                f"file of {size} bytes exceeds the plan limit of {max_size}"
            )
        source = _SourceReader(stream)
        if size == 0:
            if await source.readinto(memoryview(bytearray(1))):
                raise InternxtError("stream is longer than the announced 0 bytes")
            return await self._create_file_entry(
                plain_name, file_type, folder_uuid, 0, None
            )

        bucket = self.user.bucket
        index = secrets.token_bytes(32)
        key, iv = await self._file_key_iv(index)
        cipher = crypto.ctr_cipher_at(key, iv)
        sha = hashlib.sha256()

        if size < MULTIPART_THRESHOLD:
            shard = await self._upload_single(source, size, cipher, sha, progress)
        else:
            shard = await self._upload_multipart(
                source, size, cipher, sha, progress, part_size
            )

        shard["hash"] = crypto.file_hash(sha.digest())
        finish = await self._request(
            "POST",
            f"{self._network}/v2/buckets/{bucket}/files/finish",
            headers=self._basic(),
            json={"index": index.hex(), "shards": [shard]},
            timeout=self._slow_timeout,
        )
        return await self._create_file_entry(
            plain_name, file_type, folder_uuid, size, finish["id"]
        )

    async def upload_bytes(
        self, data: bytes, *, folder_uuid: str, name: str
    ) -> DriveFile:
        async def gen() -> AsyncIterator[bytes]:
            yield data

        return await self.upload(
            gen(), size=len(data), folder_uuid=folder_uuid, name=name
        )

    async def _max_upload_size_or_none(self) -> int | None:
        try:
            return await self.max_upload_size()
        except InternxtAuthError:
            raise
        except InternxtError as err:  # the limit check must not block a backup
            _LOGGER.debug("Could not read upload limit: %s", err)
            return None

    async def _start_upload(self, size: int, parts: int) -> dict[str, Any]:
        data = await self._request(
            "POST",
            f"{self._network}/v2/buckets/{self.user.bucket}/files/start",
            headers=self._basic(),
            params={"multiparts": str(parts)},
            json={"uploads": [{"index": 0, "size": size}]},
        )
        uploads = data.get("uploads") or []
        if len(uploads) != 1:
            raise InternxtError(
                f"start upload: expected 1 upload entry, got {len(uploads)}"
            )
        return dict(uploads[0])

    async def _upload_single(
        self,
        source: _SourceReader,
        size: int,
        cipher: Any,
        sha: Any,
        progress: ProgressCallback | None,
    ) -> dict[str, Any]:
        upload = await self._start_upload(size, 1)
        url = (upload.get("urls") or [upload.get("url")])[0]
        if not url:
            raise InternxtError("start upload: no upload URL")
        sent = 0
        body_error: InternxtError | None = None

        async def body() -> AsyncIterator[bytes]:
            nonlocal sent, body_error
            plain = bytearray(CRYPTO_BLOCK)
            out = bytearray(CRYPTO_BLOCK + 15)
            try:
                while n := await source.readinto(memoryview(plain)):
                    sent += n
                    if sent > size:
                        raise InternxtError(
                            f"stream is longer than the announced {size} bytes"
                        )
                    # copy: the transport may still hold the previous block while we refill
                    yield bytes(await _encrypt_into(cipher, sha, plain, out, n))
                    if progress:
                        progress(sent)
                if sent != size:
                    raise InternxtError(f"stream ended after {sent} of {size} bytes")
            except InternxtError as err:
                # Raising inside the body aborts the request; remember the real reason
                # because aiohttp wraps it into its own connection error.
                body_error = err
                raise

        try:
            await self._put(url, body(), size)
        except InternxtError:
            if body_error is not None:
                # keep body_error's own cause (e.g. the source's OSError), hide aiohttp's
                # wrapper, whose message contains the presigned URL
                body_error.__suppress_context__ = True
                raise body_error  # noqa: B904
            raise
        return {"uuid": upload["uuid"]}

    async def _upload_multipart(
        self,
        source: _SourceReader,
        size: int,
        cipher: Any,
        sha: Any,
        progress: ProgressCallback | None,
        part_size: int,
    ) -> dict[str, Any]:
        part_size = max(part_size, MIN_PART_SIZE, math.ceil(size / MAX_PARTS))
        num_parts = math.ceil(size / part_size)
        upload = await self._start_upload(size, num_parts)
        urls = upload.get("urls") or []
        upload_id = upload.get("UploadId")
        if len(urls) != num_parts:
            raise InternxtError(
                f"start upload: expected {num_parts} URLs, got {len(urls)}"
            )
        if not upload_id:
            raise InternxtError("start upload: no UploadId for multipart upload")

        # Two fixed buffers for the whole upload keep RAM flat on small devices.
        plain = bytearray(part_size)
        out = bytearray(part_size + 15)
        parts: list[dict[str, Any]] = []
        sent = 0
        for number, url in enumerate(urls, start=1):
            expected = min(part_size, size - sent)
            n = await source.readinto(memoryview(plain)[:expected])
            if n != expected:
                raise InternxtError(f"stream ended after {sent + n} of {size} bytes")
            encrypted = await _encrypt_into(cipher, sha, plain, out, n)
            etag = await self._put_part(url, encrypted)
            parts.append({"PartNumber": number, "ETag": etag})
            sent += n
            if progress:
                progress(sent)
        if await source.readinto(memoryview(bytearray(1))):
            raise InternxtError(f"stream is longer than the announced {size} bytes")
        return {"uuid": upload["uuid"], "UploadId": upload_id, "parts": parts}

    async def _put(self, url: str, data: Any, size: int) -> str:
        headers = self._headers(
            {"Content-Type": "application/octet-stream", "Content-Length": str(size)}
        )
        try:
            async with self._session.put(
                url, data=data, headers=headers, timeout=self._transfer_timeout
            ) as resp:
                if resp.status >= 400:
                    await _raise_for_status(resp, "PUT upload", storage=True)
                return resp.headers.get("ETag", "").strip('"')
        except TimeoutError as err:
            raise InternxtConnectionError("timeout during upload") from err
        except aiohttp.ClientError as err:
            # The message would contain the presigned URL including its signature.
            raise InternxtConnectionError(
                f"upload failed: {type(err).__name__}"
            ) from None

    async def _put_part(self, url: str, data: memoryview) -> str:
        for attempt in range(1, RETRIES + 1):
            try:
                return await self._put(url, data, len(data))
            except InternxtConnectionError as err:
                if attempt == RETRIES:
                    raise
                wait = _retry_wait(attempt, err)
                _LOGGER.debug("Part upload failed (%s), retry in %.1fs", err, wait)
                await asyncio.sleep(wait)
        raise AssertionError("unreachable")

    async def _create_file_entry(
        self,
        plain_name: str,
        file_type: str,
        folder_uuid: str,
        size: int,
        file_id: str | None,
    ) -> DriveFile:
        now = _now_iso()
        body: dict[str, Any] = {
            "name": plain_name,
            "plainName": plain_name,
            "type": file_type,
            "bucket": self.user.bucket,
            "encryptVersion": "03-aes",
            "folderUuid": folder_uuid,
            "size": size,
            "creationTime": now,
            "date": now,
            "modificationTime": now,
        }
        if file_id:
            body["fileId"] = file_id
        try:
            data = await self._request(
                "POST",
                f"{self._drive}/files",
                headers={**self._bearer(), "internxt-version": "v1.0.436"},
                json=body,
                timeout=self._slow_timeout,
            )
        except (InternxtConnectionError, InternxtConflictError) as err:
            # Like rclone: the entry may have been created although we saw no answer.
            existing = await self._find_file(folder_uuid, plain_name, file_type)
            if existing and existing.size == size and existing.file_id == file_id:
                _LOGGER.debug(
                    "File entry exists after %s, using it", type(err).__name__
                )
                return existing
            raise
        return DriveFile.from_api(data)

    async def _find_file(
        self, folder_uuid: str, plain_name: str, file_type: str
    ) -> DriveFile | None:
        try:
            files = await self.list_files(folder_uuid)
        except InternxtError:
            return None
        for f in files:
            if f.plain_name == plain_name and f.type == file_type:
                return f
        return None

    @_translate_unexpected
    async def download(self, file: DriveFile) -> DownloadStream:
        """Return a stream of the decrypted file; iterate it or close it with `aclose()`.

        The hash is checked after the last byte; a mismatch raises
        InternxtIntegrityError instead of ending the iteration, so discard the data.
        """
        if not file.file_id or file.size == 0:
            return DownloadStream(None, None, "")
        bucket = self.user.bucket
        info = await self._request(
            "GET",
            f"{self._network}/buckets/{bucket}/files/{file.file_id}/info",
            headers=self._basic(),
        )
        shards = info.get("shards") or []
        if len(shards) != 1 or not shards[0].get("url"):
            raise InternxtError(
                f"no single downloadable shard in file info (version {info.get('version')}, "
                f"{len(shards)} shards); legacy files are not supported"
            )
        key, iv = await self._file_key_iv(bytes.fromhex(info["index"]))
        try:
            resp = await self._session.get(
                shards[0]["url"],
                headers=self._headers(),
                timeout=self._transfer_timeout,
            )
        except TimeoutError as err:
            raise InternxtConnectionError("timeout starting download") from err
        except aiohttp.ClientError as err:
            raise InternxtConnectionError(
                f"download failed: {type(err).__name__}"
            ) from None
        if resp.status >= 400:
            try:
                await _raise_for_status(resp, "GET download", storage=True)
            finally:
                resp.release()
        return DownloadStream(resp, crypto.ctr_cipher_at(key, iv), shards[0]["hash"])

    async def download_bytes(self, file: DriveFile) -> bytes:
        stream = await self.download(file)
        async with stream:
            return b"".join([chunk async for chunk in stream])


# ---------------------------------------------------------------------- streams


class DownloadStream:
    """Decrypting download stream. Releases the connection when exhausted or closed."""

    def __init__(
        self, resp: aiohttp.ClientResponse | None, cipher: Any, expected_hash: str
    ) -> None:
        self._resp = resp
        self._cipher = cipher
        self._expected_hash = expected_hash
        self._sha = hashlib.sha256()
        self._done = resp is None

    def __aiter__(self) -> DownloadStream:
        return self

    async def __anext__(self) -> bytes:
        if self._done or self._resp is None:
            raise StopAsyncIteration
        try:
            block = await self._read_block()
        except TimeoutError as err:
            self._release()
            raise InternxtConnectionError("timeout during download") from err
        except aiohttp.ClientError as err:
            self._release()
            raise InternxtConnectionError(
                f"download interrupted: {type(err).__name__}"
            ) from None
        except BaseException:
            self._release()
            raise
        if not block:
            self._release()
            if crypto.file_hash(self._sha.digest()) != self._expected_hash:
                raise InternxtIntegrityError(
                    "downloaded data does not match the stored hash"
                )
            raise StopAsyncIteration
        if len(block) >= THREAD_THRESHOLD:
            return await asyncio.to_thread(
                _decrypt_sync, self._cipher, self._sha, block
            )
        return _decrypt_sync(self._cipher, self._sha, block)

    async def _read_block(self) -> bytes:
        assert self._resp is not None
        buf = bytearray()
        while len(buf) < CRYPTO_BLOCK:
            chunk = await self._resp.content.read(CRYPTO_BLOCK - len(buf))
            if not chunk:
                break
            buf += chunk
        return bytes(buf)

    def _release(self) -> None:
        if not self._done:
            self._done = True
            if self._resp is not None:
                self._resp.release()

    async def aclose(self) -> None:
        self._release()

    async def __aenter__(self) -> DownloadStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._release()

    def __del__(self) -> None:
        if not self._done and self._resp is not None:
            self._resp.release()


class _SourceReader:
    """Reads a caller's async byte stream into fixed buffers."""

    def __init__(self, stream: AsyncIterator[bytes]) -> None:
        self._it = stream.__aiter__()
        self._pending = memoryview(b"")
        self._eof = False

    async def readinto(self, buf: memoryview) -> int:
        filled = 0
        while filled < len(buf):
            if not len(self._pending):
                if self._eof:
                    break
                try:
                    chunk = await self._it.__anext__()
                except StopAsyncIteration:
                    self._eof = True
                    break
                except InternxtError:
                    raise
                except Exception as err:
                    raise InternxtStreamError(
                        f"reading the source stream failed: {type(err).__name__}"
                    ) from err
                self._pending = memoryview(chunk)
                continue
            n = min(len(buf) - filled, len(self._pending))
            buf[filled : filled + n] = self._pending[:n]
            self._pending = self._pending[n:]
            filled += n
        return filled


def _encrypt_into_sync(
    cipher: Any, sha: Any, plain: bytearray, out: bytearray, n: int
) -> memoryview:
    written = cipher.update_into(memoryview(plain)[:n], out)
    encrypted = memoryview(out)[:written]
    sha.update(encrypted)
    return encrypted


async def _encrypt_into(
    cipher: Any, sha: Any, plain: bytearray, out: bytearray, n: int
) -> memoryview:
    # AES-CTR and SHA-256 release the GIL; on a Raspberry Pi 1 MiB takes several ms.
    if n >= THREAD_THRESHOLD:
        return await asyncio.to_thread(_encrypt_into_sync, cipher, sha, plain, out, n)
    return _encrypt_into_sync(cipher, sha, plain, out, n)


def _decrypt_sync(cipher: Any, sha: Any, block: bytes) -> bytes:
    sha.update(block)
    return bytes(cipher.update(block))


# ---------------------------------------------------------------------- errors

_XML_CODE = re.compile(r"<Code>([^<]{1,100})</Code>")


async def _raise_for_status(
    resp: aiohttp.ClientResponse, what: str, *, storage: bool = False
) -> None:
    status = resp.status
    try:
        text = await resp.text()
    except (aiohttp.ClientError, UnicodeDecodeError, TimeoutError):
        text = ""
    body: Any = None
    with contextlib.suppress(ValueError):
        body = jsonlib.loads(text) if text else None
    error_code = body.get("error") if isinstance(body, dict) else None
    if isinstance(body, dict):
        message: Any = body.get("message") or body.get("error")
    elif storage:
        # S3 error bodies can echo the signature; keep only the error code.
        match = _XML_CODE.search(text)
        message = match.group(1) if match else ""
    else:
        message = text[:200]
    if isinstance(message, list):
        message = "; ".join(map(str, message))
    msg = f"{what}: HTTP {status}: {message}"

    if status == 401:
        if isinstance(message, str) and "2-factor" in message:
            raise InternxtTwoFactorRequired(msg)
        raise InternxtAuthError(msg)
    if status == 402:
        if error_code == "FILE_UPLOAD_SIZE_EXCEEDED":
            raise InternxtQuotaError(msg)
        raise InternxtPlanError(msg)
    if status == 403 and error_code == "ACCOUNT_BLOCKED":
        raise InternxtAccountBlocked(msg)
    if status == 404:
        raise InternxtNotFoundError(msg)
    if status == 409:
        raise InternxtConflictError(msg)
    if status in (413, 420):  # 420 = "max space used" on the network API
        raise InternxtQuotaError(msg)
    if status == 429:
        retry = resp.headers.get("Retry-After", "")
        raise InternxtRateLimitError(msg, float(retry) if retry.isdigit() else None)
    if status == 408 or status >= 500:
        raise InternxtConnectionError(msg)
    raise InternxtError(msg)
