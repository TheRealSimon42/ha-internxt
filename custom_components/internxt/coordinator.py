"""Storage usage coordinator for Internxt."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aiointernxt import InternxtAuthError, InternxtClient, InternxtError
from .const import DOMAIN, STORAGE_UPDATE_INTERVAL

if TYPE_CHECKING:
    from . import InternxtConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StorageInfo:
    """Storage usage of the Internxt account in bytes."""

    used: int
    total: int

    @property
    def free(self) -> int:
        return max(self.total - self.used, 0)


class InternxtStorageCoordinator(DataUpdateCoordinator[StorageInfo]):
    """Poll storage usage; backups never depend on it."""

    config_entry: InternxtConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: InternxtConfigEntry, client: InternxtClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=STORAGE_UPDATE_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> StorageInfo:
        try:
            usage, limit = await asyncio.gather(
                self.client.usage(), self.client.limit()
            )
            return StorageInfo(
                used=int(usage["total"]), total=int(limit["maxSpaceBytes"])
            )
        except InternxtAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except (InternxtError, KeyError, TypeError, ValueError) as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
