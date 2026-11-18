"""Diagnostics for Internxt."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant

from . import InternxtConfigEntry
from .aiointernxt import InternxtError
from .const import CONF_MNEMONIC, CONF_TOKEN

TO_REDACT = {CONF_EMAIL, CONF_TOKEN, CONF_MNEMONIC, "title", "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: InternxtConfigEntry
) -> dict[str, Any]:
    """Return diagnostics without secrets (e-mail, token and account key are redacted)."""
    result: dict[str, Any] = {"entry": async_redact_data(entry.as_dict(), TO_REDACT)}
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return result
    client = runtime.client
    expires = client.token_expires_at
    result["token_expires_at"] = expires.isoformat() if expires else None
    try:
        usage, limit = await client.usage(), await client.limit()
        result["storage"] = {
            "used_bytes": usage.get("total"),
            "limit_bytes": limit.get("maxSpaceBytes"),
        }
        result["max_upload_size"] = await client.max_upload_size()
        files = await client.list_files(runtime.folder_uuid)
        result["backup_folder"] = {
            "files": len(files),
            "tar_files": sum(1 for f in files if f.type == "tar"),
            "total_bytes": sum(f.size for f in files),
        }
    except InternxtError as err:
        result["error"] = f"{type(err).__name__}: {err}"
    return result
