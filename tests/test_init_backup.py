"""Tests for setup, the backup agent and diagnostics."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.backup import (
    AddonInfo,
    AgentBackup,
    BackupAgentError,
    BackupNotFound,
    Folder,
    async_get_manager,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.internxt.aiointernxt import (
    InternxtAuthError,
    InternxtConnectionError,
    InternxtPlanError,
)
from custom_components.internxt.const import CONF_TOKEN, DEFAULT_FOLDER_NAME
from custom_components.internxt.diagnostics import async_get_config_entry_diagnostics

from .conftest import EMAIL, MNEMONIC, FakeClient

BACKUP = AgentBackup(
    addons=[AddonInfo(name="Test", slug="test", version="1.0.0")],
    backup_id="abc123",
    database_included=True,
    date="2026-10-20T12:00:00.000Z",
    extra_metadata={"with_automatic_settings": True},
    folders=[Folder.MEDIA],
    homeassistant_included=True,
    homeassistant_version="2026.10.2",
    name="Automatic backup 2026.10.2",
    protected=True,
    size=12_345,
)
DATA = bytes(range(256)) * 48 + b"x" * 57  # 12_345 bytes


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, fake_client: FakeClient, config_entry: MockConfigEntry
) -> MockConfigEntry:
    with patch("homeassistant.components.backup.is_hassio", return_value=False):
        assert await async_setup_component(hass, "backup", {})
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


def _agent(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    return async_get_manager(hass).backup_agents[f"internxt.{entry.entry_id}"]


async def _stream() -> AsyncIterator[bytes]:
    for i in range(0, len(DATA), 4000):
        yield DATA[i : i + 4000]


async def _upload(agent: Any) -> list[int]:
    progress: list[int] = []

    async def open_stream() -> AsyncIterator[bytes]:
        return _stream()

    await agent.async_upload_backup(
        open_stream=open_stream,
        backup=BACKUP,
        on_progress=lambda *, bytes_uploaded, **kw: progress.append(bytes_uploaded),
    )
    return progress


async def test_setup_rotates_and_stores_token(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    entry = setup_integration
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_TOKEN] == "token-1"
    assert [f.name for f in fake_client.folders.values()] == [DEFAULT_FOLDER_NAME]
    assert f"internxt.{entry.entry_id}" in async_get_manager(hass).backup_agents


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (InternxtAuthError("401"), ConfigEntryState.SETUP_ERROR),
        (InternxtPlanError("402"), ConfigEntryState.SETUP_ERROR),
        (InternxtConnectionError("down"), ConfigEntryState.SETUP_RETRY),
    ],
)
async def test_setup_errors(
    hass: HomeAssistant,
    fake_client: FakeClient,
    config_entry: MockConfigEntry,
    error: Exception,
    state: ConfigEntryState,
) -> None:
    fake_client.refresh_error = error
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is state
    flows = hass.config_entries.flow.async_progress()
    # only a rejected login asks for the password again; a plan problem does not
    assert bool(flows) is isinstance(error, InternxtAuthError)


async def test_upload_list_download_delete(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    agent = _agent(hass, setup_integration)
    progress = await _upload(agent)
    assert progress[-1] == BACKUP.size
    names = sorted(f.name for f in fake_client.files.values())
    assert names == [
        "Automatic_backup_2026.10.2_2026-10-20_12.00_00000000.metadata.json",
        "Automatic_backup_2026.10.2_2026-10-20_12.00_00000000.tar",
    ]

    # a fresh agent reads everything back from Internxt, not from its own cache
    agent = _agent(hass, setup_integration)
    agent._cache_expires = 0
    agent._recent.clear()
    assert await agent.async_list_backups() == [BACKUP]
    assert await agent.async_get_backup("abc123") == BACKUP
    stream = await agent.async_download_backup("abc123")
    assert b"".join([c async for c in stream]) == DATA

    await agent.async_delete_backup("abc123")
    assert fake_client.files == {}
    with pytest.raises(BackupNotFound):
        await agent.async_get_backup("abc123")
    with pytest.raises(BackupNotFound):
        await agent.async_delete_backup("abc123")


async def test_metadata_failure_removes_tar(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    agent = _agent(hass, setup_integration)
    fake_client.upload_error_for = ".json"
    with pytest.raises(BackupAgentError):
        await _upload(agent)
    assert fake_client.files == {}


async def test_quota_check_before_upload(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    agent = _agent(hass, setup_integration)
    fake_client.usage_total = fake_client.limit_total - 100
    with pytest.raises(BackupAgentError) as err:
        await _upload(agent)
    assert err.value.translation_key == "quota_exceeded"
    assert fake_client.files == {}


async def test_auth_error_starts_reauth(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    agent = _agent(hass, setup_integration)

    async def fail(folder_uuid: str) -> None:
        raise InternxtAuthError("401")

    agent._cache_expires = 0
    with patch.object(fake_client, "list_files", fail), pytest.raises(BackupAgentError):
        await agent.async_list_backups()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_diagnostics_has_no_secrets(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    await _upload(_agent(hass, setup_integration))
    diag = await async_get_config_entry_diagnostics(hass, setup_integration)
    text = repr(diag)
    for secret in (EMAIL, MNEMONIC, setup_integration.data[CONF_TOKEN]):
        assert secret not in text
    assert diag["backup_folder"]["tar_files"] == 1


async def test_unload(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    assert setup_integration.state is ConfigEntryState.NOT_LOADED
    assert (
        f"internxt.{setup_integration.entry_id}"
        not in async_get_manager(hass).backup_agents
    )
