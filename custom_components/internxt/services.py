"""Actions for Internxt: upload files from Home Assistant, delete files in Internxt."""

from __future__ import annotations

from collections.abc import AsyncIterator
import logging
from pathlib import Path, PurePosixPath
from typing import IO, Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_FILENAME
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from . import InternxtConfigEntry, async_ensure_fresh_token
from .aiointernxt import InternxtError, InternxtNotFoundError
from .const import DEFAULT_UPLOAD_FOLDER, DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_CONFIG_ENTRY_ID = "config_entry_id"
CONF_DESTINATION_FOLDER = "destination_folder"
CONF_OVERWRITE = "overwrite"
CONF_PATH = "path"
CONF_PERMANENTLY = "permanently"

READ_CHUNK = 1024 * 1024

UPLOAD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONFIG_ENTRY_ID): cv.string,
        vol.Required(CONF_FILENAME): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(CONF_DESTINATION_FOLDER, default=DEFAULT_UPLOAD_FOLDER): cv.string,
        vol.Optional(CONF_OVERWRITE, default=False): cv.boolean,
    }
)
DELETE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONFIG_ENTRY_ID): cv.string,
        vol.Required(CONF_PATH): vol.All(
            cv.ensure_list, vol.Length(min=1), [cv.string]
        ),
        vol.Optional(CONF_PERMANENTLY, default=False): cv.boolean,
    }
)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Internxt actions."""

    async def upload(call: ServiceCall) -> ServiceResponse:
        return await _async_upload(hass, call)

    async def delete(call: ServiceCall) -> ServiceResponse:
        return await _async_delete(hass, call)

    hass.services.async_register(
        DOMAIN,
        "upload",
        upload,
        schema=UPLOAD_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "delete",
        delete,
        schema=DELETE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def _loaded_entry(hass: HomeAssistant, entry_id: str) -> InternxtConfigEntry:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_found",
            translation_placeholders={"entry_id": entry_id},
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
            translation_placeholders={"title": entry.title},
        )
    return entry


def _check_files(hass: HomeAssistant, filenames: list[str]) -> list[tuple[Path, int]]:
    """Validate paths (allowlist, existence) and return them with their sizes."""
    result: list[tuple[Path, int]] = []
    for name in filenames:
        if not hass.config.is_allowed_path(name):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_access_to_path",
                translation_placeholders={"filename": name},
            )
        path = Path(name)
        if not path.is_file():
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="file_not_found",
                translation_placeholders={"filename": name},
            )
        result.append((path, path.stat().st_size))
    return result


async def _read_file(hass: HomeAssistant, path: Path) -> AsyncIterator[bytes]:
    """Stream a local file without blocking the event loop."""
    handle: IO[bytes] = await hass.async_add_executor_job(path.open, "rb")
    try:
        while chunk := await hass.async_add_executor_job(handle.read, READ_CHUNK):
            yield chunk
    finally:
        await hass.async_add_executor_job(handle.close)


def _raise_internxt(err: InternxtError, key: str, **placeholders: str) -> None:
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={**placeholders, "error": str(err)},
    ) from err


async def _async_upload(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    entry = _loaded_entry(hass, call.data[CONF_CONFIG_ENTRY_ID])
    files = await hass.async_add_executor_job(
        _check_files, hass, call.data[CONF_FILENAME]
    )
    client = entry.runtime_data.client
    folder_path = call.data[CONF_DESTINATION_FOLDER].strip("/")
    uploaded: list[dict[str, Any]] = []
    current = ""
    try:
        await async_ensure_fresh_token(hass, entry)
        folder_uuid = await client.ensure_folder_path(
            client.user.root_folder_uuid, folder_path
        )
        for path, size in files:
            current = str(path)
            existing = await client.find_file(folder_uuid, path.name)
            if existing is not None:
                if not call.data[CONF_OVERWRITE]:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN,
                        translation_key="file_exists",
                        translation_placeholders={"path": f"{folder_path}/{path.name}"},
                    )
                await client.trash_file(existing.uuid)  # old version stays in the trash
            result = await client.upload(
                _read_file(hass, path),
                size=size,
                folder_uuid=folder_uuid,
                name=path.name,
            )
            uploaded.append(
                {
                    "path": f"{folder_path}/{result.name}",
                    "size": result.size,
                    "uuid": result.uuid,
                }
            )
    except InternxtError as err:
        _raise_internxt(err, "upload_failed", filename=current or folder_path)
    _LOGGER.debug(
        "Uploaded %d file(s) to Internxt folder %s", len(uploaded), folder_path
    )
    return {"files": uploaded}


async def _async_delete(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    entry = _loaded_entry(hass, call.data[CONF_CONFIG_ENTRY_ID])
    client = entry.runtime_data.client
    deleted: list[str] = []
    for raw in call.data[CONF_PATH]:
        target = PurePosixPath("/" + raw.strip("/"))
        try:
            folder_uuid = await client.find_folder_path(
                client.user.root_folder_uuid, str(target.parent)
            )
            file = (
                await client.find_file(folder_uuid, target.name)
                if folder_uuid
                else None
            )
            if file is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="path_not_found",
                    translation_placeholders={"path": raw},
                )
            if call.data[CONF_PERMANENTLY]:
                await client.delete_file(file.uuid)
            else:
                await client.trash_file(file.uuid)
        except InternxtNotFoundError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="path_not_found",
                translation_placeholders={"path": raw},
            ) from err
        except InternxtError as err:
            _raise_internxt(err, "delete_failed", path=raw)
        deleted.append(str(target).lstrip("/"))
    return {"deleted": deleted}
