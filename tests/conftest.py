"""Fixtures for the Internxt integration tests.

The library itself is tested against a fake HTTP server in aiointernxt/tests; here an
in-memory client stands in for it, so the tests cover the Home Assistant side.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.internxt.aiointernxt import (
    Credentials,
    DriveFile,
    DriveFolder,
    InternxtAuthError,
    InternxtTwoFactorRequired,
    SecurityDetails,
    User,
)
from custom_components.internxt.const import (
    CONF_FOLDER_NAME,
    CONF_MNEMONIC,
    CONF_TOKEN,
    DEFAULT_FOLDER_NAME,
    DOMAIN,
)

from .common import MockConfigEntry

EMAIL = "simon@example.com"
PASSWORD = "secret password"
MNEMONIC = " ".join(["abandon"] * 11 + ["about"])
USER = User(
    uuid="3f1c1b9e-0000-4000-8000-000000000001",
    email=EMAIL,
    user_id="bridge-user",
    bridge_user=EMAIL,
    bucket="0123456789abcdef01234567",
    root_folder_uuid="root",
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request: pytest.FixtureRequest) -> None:
    """Load integrations from custom_components (pytest-homeassistant does that by default)."""
    if "enable_custom_integrations" in request.fixturenames or _has_fixture(
        request, "enable_custom_integrations"
    ):
        request.getfixturevalue("enable_custom_integrations")


def _has_fixture(request: pytest.FixtureRequest, name: str) -> bool:
    return bool(request._fixturemanager.getfixturedefs(name, request.node))


class FakeClient:
    """In-memory stand-in for InternxtClient with switches for error cases."""

    def __init__(self) -> None:
        self.two_factor = False
        self.login_error: Exception | None = None
        self.refresh_error: Exception | None = None
        self.upload_error_for: str | None = None  # file name suffix that fails
        self.usage_total = 1_000
        self.limit_total = 10**12
        self._ids = itertools.count(1)
        self._token_no = 0
        self.credentials_value: Credentials | None = None
        self._user: User | None = None
        self.folders: dict[str, DriveFolder] = {}
        self.files: dict[str, DriveFile] = {}
        self.data: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.trashed: list[str] = []
        self.stream_fail_after: int | None = None
        self.download_ranges: list[tuple[int, int | None]] = []
        self.refresh_calls = 0

    # session-bound constructor signature
    def __call__(self, *args: Any, **kwargs: Any) -> FakeClient:
        return self

    @property
    def credentials(self) -> Credentials:
        if self.credentials_value is None:
            raise InternxtAuthError("not logged in")
        return self.credentials_value

    @property
    def user(self) -> User:
        assert self._user is not None
        return self._user

    @property
    def token_expires_at(self) -> datetime:
        return datetime.now(UTC) + timedelta(days=3)

    def set_credentials(self, credentials: Credentials) -> None:
        self.credentials_value = credentials

    async def security_details(self, email: str) -> SecurityDetails:
        return SecurityDetails(two_factor=self.two_factor, encrypted_salt="x")

    async def login(
        self, email: str, password: str, tfa_code: str | None = None
    ) -> Credentials:
        if self.login_error:
            raise self.login_error
        if self.two_factor and tfa_code != "123456":
            raise InternxtTwoFactorRequired("Wrong 2-factor auth code")
        if password != PASSWORD:
            raise InternxtAuthError("Wrong login credentials")
        self.credentials_value = Credentials(token="token-login", mnemonic=MNEMONIC)
        self._user = USER
        return self.credentials_value

    async def refresh(self) -> User:
        self.refresh_calls += 1
        if self.refresh_error:
            raise self.refresh_error
        self._token_no += 1
        self.credentials_value = Credentials(
            token=f"token-{self._token_no}", mnemonic=self.credentials.mnemonic
        )
        self._user = USER
        return USER

    async def usage(self) -> dict[str, int]:
        return {"drive": self.usage_total, "total": self.usage_total}

    async def limit(self) -> dict[str, int]:
        return {"maxSpaceBytes": self.limit_total}

    async def max_upload_size(self) -> int:
        return 100 * 10**9

    async def ensure_folder(self, parent_uuid: str, name: str) -> DriveFolder:
        for folder in self.folders.values():
            if folder.name == name and folder.parent_uuid == parent_uuid:
                return folder
        folder = DriveFolder(
            uuid=f"folder-{next(self._ids)}", name=name, parent_uuid=parent_uuid
        )
        self.folders[folder.uuid] = folder
        return folder

    async def list_files(self, folder_uuid: str) -> list[DriveFile]:
        return [f for f in self.files.values() if f.folder_uuid == folder_uuid]

    async def list_folders(self, folder_uuid: str) -> list[DriveFolder]:
        return [f for f in self.folders.values() if f.parent_uuid == folder_uuid]

    async def ensure_folder_path(self, parent_uuid: str, path: str) -> str:
        current = parent_uuid
        for part in (p for p in path.strip("/").split("/") if p):
            current = (await self.ensure_folder(current, part)).uuid
        return current

    async def find_folder_path(self, parent_uuid: str, path: str) -> str | None:
        current = parent_uuid
        for part in (p for p in path.strip("/").split("/") if p):
            match = next(
                (f for f in await self.list_folders(current) if f.name == part), None
            )
            if match is None:
                return None
            current = match.uuid
        return current

    async def find_file(self, folder_uuid: str, name: str) -> DriveFile | None:
        return next(
            (f for f in await self.list_files(folder_uuid) if f.name == name), None
        )

    async def file_meta(self, file_uuid: str) -> DriveFile:
        from custom_components.internxt.aiointernxt import InternxtNotFoundError

        if file_uuid not in self.files:
            raise InternxtNotFoundError("not found")
        return self.files[file_uuid]

    async def rename_file(self, file_uuid: str, name: str) -> None:
        import dataclasses

        plain, _, ext = name.rpartition(".")
        self.files[file_uuid] = dataclasses.replace(
            self.files[file_uuid], plain_name=plain, type=ext
        )

    async def trash_file(self, file_uuid: str) -> None:
        self.trashed.append(file_uuid)
        del self.files[file_uuid]

    async def upload(
        self, stream: AsyncIterator[bytes], *, size: int, folder_uuid: str, name: str,
        progress: Any = None, part_size: int = 0,
    ) -> DriveFile:  # fmt: skip
        if self.upload_error_for and name.endswith(self.upload_error_for):
            from custom_components.internxt.aiointernxt import InternxtConnectionError

            raise InternxtConnectionError("upload failed")
        data = b"".join([chunk async for chunk in stream])
        assert len(data) == size
        if progress:
            progress(size)
        plain, _, ext = name.rpartition(".")
        n = next(self._ids)
        f = DriveFile(
            uuid=f"file-{n}", file_id=f"{n:024x}", plain_name=plain, type=ext, size=size,
            folder_uuid=folder_uuid, modification_time=None,
        )  # fmt: skip
        self.files[f.uuid] = f
        self.data[f.uuid] = data
        return f

    async def upload_bytes(
        self, data: bytes, *, folder_uuid: str, name: str
    ) -> DriveFile:
        async def gen() -> AsyncIterator[bytes]:
            yield data

        return await self.upload(
            gen(), size=len(data), folder_uuid=folder_uuid, name=name
        )

    async def download(
        self, file: DriveFile, *, start: int = 0, end: int | None = None
    ) -> FakeStream:
        from custom_components.internxt.aiointernxt import InternxtNotFoundError

        self.download_ranges.append((start, end))
        if file.uuid not in self.files:
            raise InternxtNotFoundError("gone")
        data = self.data[file.uuid]
        return FakeStream(
            data[start : None if end is None else end + 1], self.stream_fail_after
        )

    async def download_bytes(self, file: DriveFile) -> bytes:
        return self.data[file.uuid]

    async def delete_file(self, file_uuid: str) -> None:
        from custom_components.internxt.aiointernxt import InternxtNotFoundError

        if file_uuid not in self.files:
            raise InternxtNotFoundError("not found")
        del self.files[file_uuid]
        self.deleted.append(file_uuid)


class FakeStream:
    """Mimics aiointernxt.DownloadStream."""

    def __init__(self, data: bytes, fail_after: int | None = None) -> None:
        self._chunks = [data[i : i + 1000] for i in range(0, len(data), 1000)]
        self.closed = False
        self._fail_after = fail_after

    def __aiter__(self) -> FakeStream:
        return self

    async def __anext__(self) -> bytes:
        if not self._chunks:
            raise StopAsyncIteration
        if self._fail_after is not None:
            if self._fail_after == 0:
                from custom_components.internxt.aiointernxt import (
                    InternxtConnectionError,
                )

                raise InternxtConnectionError("connection lost")
            self._fail_after -= 1
        return self._chunks.pop(0)

    async def aclose(self) -> None:
        self.closed = True

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True


@pytest.fixture
def fake_client() -> Generator[FakeClient]:
    """Patch the library client everywhere the integration creates one."""
    client = FakeClient()
    with (
        patch("custom_components.internxt.InternxtClient", client),
        patch("custom_components.internxt.config_flow.InternxtClient", client),
    ):
        yield client


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A configured Internxt entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=USER.uuid,
        data={
            "email": EMAIL,
            CONF_TOKEN: "token-stored",
            CONF_MNEMONIC: MNEMONIC,
            CONF_FOLDER_NAME: DEFAULT_FOLDER_NAME,
        },
        entry_id="01JINTERNXTENTRY000000000",
    )


@pytest.fixture
async def loaded(
    hass: HomeAssistant,
    fake_client: FakeClient,
    config_entry: MockConfigEntry,
    tmp_path: Path,
) -> MockConfigEntry:
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    with patch("homeassistant.components.backup.is_hassio", return_value=False):
        assert await async_setup_component(hass, "backup", {})
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return config_entry
