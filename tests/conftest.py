"""Fixtures for the Internxt integration tests.

The library itself is tested against a fake HTTP server in aiointernxt/tests; here an
in-memory client stands in for it, so the tests cover the Home Assistant side.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime, timedelta
import itertools
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load integrations from custom_components."""


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

    async def login(self, email: str, password: str, tfa_code: str | None = None) -> Credentials:
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
        folder = DriveFolder(uuid=f"folder-{next(self._ids)}", name=name, parent_uuid=parent_uuid)
        self.folders[folder.uuid] = folder
        return folder

    async def list_files(self, folder_uuid: str) -> list[DriveFile]:
        return [f for f in self.files.values() if f.folder_uuid == folder_uuid]

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

    async def upload_bytes(self, data: bytes, *, folder_uuid: str, name: str) -> DriveFile:
        async def gen() -> AsyncIterator[bytes]:
            yield data

        return await self.upload(gen(), size=len(data), folder_uuid=folder_uuid, name=name)

    async def download(self, file: DriveFile) -> FakeStream:
        return FakeStream(self.data[file.uuid])

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

    def __init__(self, data: bytes) -> None:
        self._chunks = [data[i : i + 1000] for i in range(0, len(data), 1000)]
        self.closed = False

    def __aiter__(self) -> FakeStream:
        return self

    async def __anext__(self) -> bytes:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def aclose(self) -> None:
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
