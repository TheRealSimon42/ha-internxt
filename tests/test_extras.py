"""Tests for the upload/delete actions, storage sensors and the media source."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.media_source import (
    URI_SCHEME,
    async_browse_media,
    async_resolve_media,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.setup import async_setup_component

from custom_components.internxt.const import DOMAIN

from .conftest import FakeClient


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
    await hass.async_block_till_done()
    return config_entry


async def _upload(
    hass: HomeAssistant, entry: MockConfigEntry, files: list[str], **extra
):
    return await hass.services.async_call(
        DOMAIN, "upload",
        {"config_entry_id": entry.entry_id, "filename": files, **extra},
        blocking=True, return_response=True,
    )  # fmt: skip


async def test_upload_creates_folders_and_streams(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: FakeClient,
    tmp_path: Path,
) -> None:
    snap = tmp_path / "front_door.jpg"
    snap.write_bytes(b"\xff\xd8" + b"x" * 3_000_000)
    response = await _upload(hass, loaded, [str(snap)], destination_folder="HA/Kameras")
    assert response["files"][0]["path"] == "HA/Kameras/front_door.jpg"
    assert response["files"][0]["size"] == snap.stat().st_size
    stored = next(f for f in fake_client.files.values() if f.name == "front_door.jpg")
    assert fake_client.data[stored.uuid] == snap.read_bytes()
    names = {f.name for f in fake_client.folders.values()}
    assert {"HA", "Kameras"} <= names


async def test_upload_default_folder_and_overwrite(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: FakeClient,
    tmp_path: Path,
) -> None:
    f = tmp_path / "a.txt"
    f.write_text("one")
    await _upload(hass, loaded, [str(f)])
    with pytest.raises(ServiceValidationError) as err:
        await _upload(hass, loaded, [str(f)])
    assert err.value.translation_key == "file_exists"
    f.write_text("two")
    response = await _upload(hass, loaded, [str(f)], overwrite=True)
    assert response["files"][0]["path"] == "Home Assistant Uploads/a.txt"
    assert len(fake_client.trashed) == 1
    current = next(x for x in fake_client.files.values() if x.name == "a.txt")
    assert fake_client.data[current.uuid] == b"two"


@pytest.mark.parametrize(
    ("make_path", "key"),
    [
        (lambda tmp: "/etc/passwd", "no_access_to_path"),
        (lambda tmp: str(tmp / "missing.jpg"), "file_not_found"),
    ],
)
async def test_upload_rejects_bad_paths(
    hass: HomeAssistant, loaded: MockConfigEntry, tmp_path: Path, make_path, key: str
) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await _upload(hass, loaded, [make_path(tmp_path)])
    assert err.value.translation_key == key


async def test_upload_error_is_translated(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: FakeClient,
    tmp_path: Path,
) -> None:
    f = tmp_path / "b.txt"
    f.write_text("x")
    fake_client.upload_error_for = ".txt"
    with pytest.raises(HomeAssistantError) as err:
        await _upload(hass, loaded, [str(f)])
    assert err.value.translation_key == "upload_failed"


async def test_delete_to_trash_and_permanently(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: FakeClient,
    tmp_path: Path,
) -> None:
    for name in ("x.jpg", "y.jpg"):
        (tmp_path / name).write_bytes(b"img")
        await _upload(hass, loaded, [str(tmp_path / name)], destination_folder="Fotos")
    response = await hass.services.async_call(
        DOMAIN, "delete",
        {"config_entry_id": loaded.entry_id, "path": ["/Fotos/x.jpg"]},
        blocking=True, return_response=True,
    )  # fmt: skip
    assert response == {"deleted": ["Fotos/x.jpg"]}
    assert len(fake_client.trashed) == 1
    await hass.services.async_call(
        DOMAIN, "delete",
        {"config_entry_id": loaded.entry_id, "path": "Fotos/y.jpg", "permanently": True},
        blocking=True,
    )  # fmt: skip
    assert len(fake_client.deleted) >= 1 and len(fake_client.trashed) == 1
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "delete",
            {"config_entry_id": loaded.entry_id, "path": "Fotos/x.jpg"},
            blocking=True,
        )  # fmt: skip
    assert err.value.translation_key == "path_not_found"


async def test_action_needs_loaded_entry(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "delete", {"config_entry_id": "nope", "path": "a"}, blocking=True
        )
    assert err.value.translation_key == "entry_not_found"


async def test_storage_sensors(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: FakeClient
) -> None:
    states = {
        s.entity_id: s
        for s in hass.states.async_all("sensor")
        if s.entity_id.startswith("sensor.internxt")
    }
    used = states["sensor.internxt_storage_used"]
    assert float(used.state) == pytest.approx(fake_client.usage_total / 1e9, abs=0.01)
    assert used.attributes["unit_of_measurement"] == "GB"
    assert float(states["sensor.internxt_storage_total"].state) == pytest.approx(1000.0)
    assert float(
        states["sensor.internxt_storage_used_percentage"].state
    ) == pytest.approx(0.0, abs=0.01)
    assert len(states) == 4


async def test_sensors_unavailable_do_not_block_setup(
    hass: HomeAssistant, fake_client: FakeClient, config_entry: MockConfigEntry
) -> None:
    async def broken() -> dict:
        from custom_components.internxt.aiointernxt import InternxtConnectionError

        raise InternxtConnectionError("down")

    fake_client.usage = broken  # type: ignore[method-assign]
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state.name == "LOADED"
    assert hass.states.get("sensor.internxt_storage_used").state == "unavailable"


async def test_media_browse_resolve_and_stream(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: FakeClient,
    tmp_path: Path,
    hass_client,
) -> None:
    assert await async_setup_component(hass, "media_source", {})
    video = tmp_path / "clip.mp4"
    video.write_bytes(bytes(range(256)) * 400)
    (tmp_path / "notes.pdf").write_bytes(b"%PDF")
    await _upload(
        hass,
        loaded,
        [str(video), str(tmp_path / "notes.pdf")],
        destination_folder="Medien",
    )

    root = await async_browse_media(hass, f"{URI_SCHEME}{DOMAIN}")
    folder = next(c for c in root.children if c.title == "Medien")
    assert folder.can_expand
    browsed = await async_browse_media(hass, folder.media_content_id)
    assert [c.title for c in browsed.children] == ["clip.mp4"]  # non-media files hidden
    assert browsed.title == "Medien"

    item = browsed.children[0]
    resolved = await async_resolve_media(hass, item.media_content_id, None)
    assert resolved.mime_type == "video/mp4"

    client = await hass_client()
    full = await client.get(resolved.url)
    assert full.status == 200
    assert await full.read() == video.read_bytes()
    part = await client.get(resolved.url, headers={"Range": "bytes=1000-1099"})
    assert part.status == 206
    assert part.headers["Content-Range"] == f"bytes 1000-1099/{video.stat().st_size}"
    assert await part.read() == video.read_bytes()[1000:1100]
    tail = await client.get(resolved.url, headers={"Range": "bytes=-10"})
    assert await tail.read() == video.read_bytes()[-10:]
    bad = await client.get(resolved.url, headers={"Range": "bytes=999999-"})
    assert bad.status == 416
    missing = await client.get(
        resolved.url.replace(item.identifier.split("/")[2], "nope")
    )
    assert missing.status == 404
