"""Constants for the Internxt integration."""

from collections.abc import Callable
from datetime import timedelta
from typing import Final

from homeassistant.util.hass_dict import HassKey

DOMAIN: Final = "internxt"

CONF_TOKEN: Final = "token"  # noqa: S105 - config key, not a secret
CONF_MNEMONIC: Final = "mnemonic"
CONF_FOLDER_NAME: Final = "folder_name"

DEFAULT_FOLDER_NAME: Final = "Home Assistant Backups"
CLIENT_NAME: Final = "ha-internxt"
UPGRADE_URL: Final = "https://internxt.com/simon42"

# The token is valid for days; refresh well before that and before long uploads.
TOKEN_REFRESH_INTERVAL: Final = timedelta(hours=12)
TOKEN_MIN_REMAINING: Final = timedelta(hours=12)

DATA_BACKUP_AGENT_LISTENERS: HassKey[list[Callable[[], None]]] = HassKey(
    f"{DOMAIN}.backup_agent_listeners"
)

DEFAULT_UPLOAD_FOLDER: Final = "Home Assistant Uploads"
STORAGE_UPDATE_INTERVAL: Final = timedelta(minutes=30)
MEDIA_VIEW_URL: Final = "/api/internxt/media"
