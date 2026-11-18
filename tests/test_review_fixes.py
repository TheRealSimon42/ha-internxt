"""Regression tests for the review of 0.2.0: media view safety, overwrite, protection."""

from __future__ import annotations

from pathlib import Path

import aiohttp
import pytest
from homeassistant.components.media_source import (
    URI_SCHEME,
    async_browse_media,
    async_resolve_media,
)
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.setup import async_setup_component

from custom_components.internxt.aiointernxt import InternxtAuthError
from custom_components.internxt.const import DOMAIN

from .common import MockConfigEntry
from .conftest import FakeClient
from .test_extras import _upload


async def _resolved_url(hass: HomeAssistant, folder: str, name: str) -> str:
    assert await async_setup_component(hass, "media_source", {})
    root = await async_browse_media(hass, f"{URI_SCHEME}{DOMAIN}")
    node = next(c for c in root.children if c.title == folder)
    item = next(
        c
        for c in (await async_browse_media(hass, node.media_content_id)).children
        if c.title == name
    )
    return (await async_resolve_media(hass, item.media_content_id, None)).url


@pytest.mark.parametrize("name", ["Urlaub #1 schön.jpg", "was?.mp4", "100% sicher.mp3"])
async def test_special_characters_in_names_play(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    tmp_path: Path,
    hass_client_no_auth,
    name: str,
) -> None:
    from homeassistant.components.media_player.browse_media import (
        async_process_play_media_url,
    )

    f = tmp_path / name
    f.write_bytes(b"media-" * 100)
    await _upload(hass, loaded, [str(f)], destination_folder="Medien")
    url = await _resolved_url(hass, "Medien", name)
    # sign it exactly like the media_source websocket API does for the frontend
    signed = async_process_play_media_url(hass, url, allow_relative_url=True)
    assert "#" not in signed and "?authSig=" in signed
    client = await hass_client_no_auth()
    resp = await client.get(signed)
    assert resp.status == 200
    assert await resp.read() == f.read_bytes()


async def test_view_ignores_media_type_from_url(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: FakeClient,
    tmp_path: Path,
    hass_client,
) -> None:
    tar = tmp_path / "backup.tar"
    tar.write_bytes(b"secret backup")
    response = await _upload(hass, loaded, [str(tar)], destination_folder="X")
    uuid = response["files"][0]["uuid"]
    assert await async_setup_component(hass, "media_source", {})
    await async_browse_media(hass, f"{URI_SCHEME}{DOMAIN}")  # registers the view
    client = await hass_client()
    resp = await client.get(f"/api/internxt/media/{loaded.entry_id}/{uuid}/fake.mp4")
    assert resp.status == 404


async def test_deleted_file_gives_404_not_stale_data(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: FakeClient,
    tmp_path: Path,
    hass_client,
) -> None:
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"v" * 5000)
    await _upload(hass, loaded, [str(f)], destination_folder="Medien")
    url = await _resolved_url(hass, "Medien", "clip.mp4")
    client = await hass_client()
    assert (await client.get(url)).status == 200  # metadata now cached
    await hass.services.async_call(
        DOMAIN, "delete",
        {"config_entry_id": loaded.entry_id, "path": "Medien/clip.mp4", "permanently": True},
        blocking=True,
    )  # fmt: skip
    assert (await client.get(url)).status == 404


async def test_interrupted_stream_fails_fast(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: FakeClient,
    tmp_path: Path,
    hass_client,
) -> None:
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"v" * 50_000)
    await _upload(hass, loaded, [str(f)], destination_folder="Medien")
    url = await _resolved_url(hass, "Medien", "clip.mp4")
    fake_client.stream_fail_after = 3
    client = await hass_client()
    resp = await client.get(url)
    assert resp.status == 200
    with pytest.raises(aiohttp.ClientPayloadError):
        await resp.read()


async def test_head_request(
    hass: HomeAssistant, loaded: MockConfigEntry, tmp_path: Path, hass_client
) -> None:
    f = tmp_path / "pic.png"
    f.write_bytes(b"p" * 1234)
    await _upload(hass, loaded, [str(f)], destination_folder="Medien")
    url = await _resolved_url(hass, "Medien", "pic.png")
    client = await hass_client()
    resp = await client.head(url)
    assert resp.status == 200
    assert resp.headers["Content-Length"] == "1234"
    assert resp.headers["Content-Type"] == "image/png"


async def test_overwrite_keeps_old_file_when_upload_fails(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: FakeClient,
    tmp_path: Path,
) -> None:
    f = tmp_path / "a.txt"
    f.write_text("old")
    await _upload(hass, loaded, [str(f)])
    f.write_text("new")
    fake_client.upload_error_for = ".part"
    with pytest.raises(HomeAssistantError):
        await _upload(hass, loaded, [str(f)], overwrite=True)
    current = [x for x in fake_client.files.values() if x.name == "a.txt"]
    assert len(current) == 1
    assert fake_client.data[current[0].uuid] == b"old"
    assert fake_client.trashed == []


@pytest.mark.parametrize(
    ("data", "key"),
    [
        ({"path": "Home Assistant Backups/x.tar"}, "backup_folder_protected"),
        ({"path": "../etc/x"}, "invalid_path"),
    ],
)
async def test_delete_guards(
    hass: HomeAssistant, loaded: MockConfigEntry, data: dict, key: str
) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "delete",
            {"config_entry_id": loaded.entry_id, **data},
            blocking=True,
        )
    assert err.value.translation_key == key


async def test_upload_guards(
    hass: HomeAssistant, loaded: MockConfigEntry, tmp_path: Path
) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.jpg").write_bytes(b"1")
    (tmp_path / "x.jpg").write_bytes(b"2")
    with pytest.raises(ServiceValidationError) as err:
        await _upload(
            hass, loaded, [str(tmp_path / "x.jpg"), str(tmp_path / "a" / "x.jpg")]
        )
    assert err.value.translation_key == "duplicate_name"
    with pytest.raises(ServiceValidationError) as err:
        await _upload(
            hass, loaded, [str(tmp_path / "x.jpg")], destination_folder="a/../b"
        )
    assert err.value.translation_key == "invalid_path"


async def test_actions_are_admin_only(
    hass: HomeAssistant, loaded: MockConfigEntry, hass_read_only_user, tmp_path: Path
) -> None:
    (tmp_path / "x.jpg").write_bytes(b"1")
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN, "upload",
            {"config_entry_id": loaded.entry_id, "filename": str(tmp_path / "x.jpg")},
            blocking=True, context=Context(user_id=hass_read_only_user.id),
        )  # fmt: skip


async def test_auth_error_in_action_starts_reauth(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: FakeClient
) -> None:
    async def fail(*args, **kwargs):
        raise InternxtAuthError("401")

    fake_client.find_folder_path = fail  # type: ignore[method-assign]
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "delete",
            {"config_entry_id": loaded.entry_id, "path": "a/b.jpg"},
            blocking=True,
        )
    assert err.value.translation_key == "auth_failed"
    await hass.async_block_till_done()
    assert [
        f["context"]["source"] for f in hass.config_entries.flow.async_progress()
    ] == ["reauth"]


async def test_overwrite_waits_until_name_is_free(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: FakeClient,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from custom_components.internxt import services
    from custom_components.internxt.aiointernxt import InternxtConflictError

    monkeypatch.setattr(services, "RENAME_RETRY_DELAYS", (0, 0, 0))
    f = tmp_path / "a.txt"
    f.write_text("old")
    await _upload(hass, loaded, [str(f)])
    f.write_text("new")
    real_rename = fake_client.rename_file
    conflicts = [1, 1]  # Internxt still sees the trashed name twice

    async def rename(uuid: str, name: str) -> None:
        if conflicts:
            conflicts.pop()
            raise InternxtConflictError("A file with this name already exists")
        await real_rename(uuid, name)

    fake_client.rename_file = rename  # type: ignore[method-assign]
    await _upload(hass, loaded, [str(f)], overwrite=True)
    current = [x for x in fake_client.files.values() if x.name == "a.txt"]
    assert len(current) == 1 and fake_client.data[current[0].uuid] == b"new"

    # still blocked after all retries: honest error naming the temporary file
    conflicts.extend([1] * 10)
    with pytest.raises(HomeAssistantError) as err:
        await _upload(hass, loaded, [str(f)], overwrite=True)
    assert err.value.translation_key == "overwrite_incomplete"
    assert ".part" in err.value.translation_placeholders["temp"]
