"""The Internxt integration: Home Assistant backups in Internxt Drive."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .aiointernxt import (
    Credentials,
    InternxtAccountError,
    InternxtAuthError,
    InternxtClient,
    InternxtError,
)
from .const import (
    CLIENT_NAME,
    CONF_FOLDER_NAME,
    CONF_MNEMONIC,
    CONF_TOKEN,
    DATA_BACKUP_AGENT_LISTENERS,
    DEFAULT_FOLDER_NAME,
    DOMAIN,
    TOKEN_MIN_REMAINING,
    TOKEN_REFRESH_INTERVAL,
    UPGRADE_URL,
)

from .coordinator import InternxtStorageCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class InternxtRuntimeData:
    """Objects shared by the backup agent, actions, sensors and media source."""

    client: InternxtClient
    folder_uuid: str
    coordinator: InternxtStorageCoordinator


type InternxtConfigEntry = ConfigEntry[InternxtRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions once for all accounts."""
    from .services import async_setup_services  # noqa: PLC0415

    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: InternxtConfigEntry) -> bool:
    """Set up Internxt from a config entry."""
    client = InternxtClient(async_get_clientsession(hass), client_name=CLIENT_NAME)
    client.set_credentials(
        Credentials(token=entry.data[CONF_TOKEN], mnemonic=entry.data[CONF_MNEMONIC])
    )
    try:
        await async_refresh_token(hass, entry, client)
        folder = await client.ensure_folder(
            client.user.root_folder_uuid,
            entry.data.get(CONF_FOLDER_NAME, DEFAULT_FOLDER_NAME),
        )
    except InternxtAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="auth_failed"
        ) from err
    except InternxtAccountError as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="account_unusable",
            translation_placeholders={"error": str(err), "upgrade_url": UPGRADE_URL},
        ) from err
    except InternxtError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="cannot_connect"
        ) from err

    coordinator = InternxtStorageCoordinator(hass, entry, client)
    # Storage sensors are extras: if usage can't be read now, they start unavailable
    # instead of blocking backups.
    await coordinator.async_refresh()
    entry.runtime_data = InternxtRuntimeData(
        client=client, folder_uuid=folder.uuid, coordinator=coordinator
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _refresh_periodically(now: datetime) -> None:
        try:
            await async_refresh_token(hass, entry, client)
        except InternxtAuthError:
            entry.async_start_reauth(hass)
        except InternxtError as err:
            _LOGGER.debug("Token refresh failed, will retry: %s", err)

    entry.async_on_unload(
        async_track_time_interval(hass, _refresh_periodically, TOKEN_REFRESH_INTERVAL)
    )

    def _notify_backup_listeners() -> None:
        for listener in hass.data.get(DATA_BACKUP_AGENT_LISTENERS, []):
            listener()

    entry.async_on_unload(entry.async_on_state_change(_notify_backup_listeners))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: InternxtConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_refresh_token(
    hass: HomeAssistant, entry: InternxtConfigEntry, client: InternxtClient
) -> None:
    """Refresh the token and store the new one right away (it changes on every refresh)."""
    await client.refresh()
    token = client.credentials.token
    if token != entry.data[CONF_TOKEN]:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TOKEN: token}
        )


async def async_ensure_fresh_token(
    hass: HomeAssistant, entry: InternxtConfigEntry
) -> None:
    """Refresh before long operations when the token expires soon."""
    client = entry.runtime_data.client
    expires = client.token_expires_at
    if expires is None or expires - dt_util.utcnow() < TOKEN_MIN_REMAINING:
        await async_refresh_token(hass, entry, client)
