"""Config flow for Internxt."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_CODE, CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .aiointernxt import (
    InternxtAccountBlocked,
    InternxtAuthError,
    InternxtClient,
    InternxtConnectionError,
    InternxtError,
    InternxtPlanError,
    InternxtTwoFactorRequired,
)
from .const import (
    CLIENT_NAME,
    CONF_FOLDER_NAME,
    CONF_MNEMONIC,
    CONF_TOKEN,
    DEFAULT_FOLDER_NAME,
    DOMAIN,
    UPGRADE_URL,
)

_LOGGER = logging.getLogger(__name__)

PREFLIGHT_FILE = "ha-internxt-connection-test.txt"

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)
STEP_PASSWORD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)
STEP_TWO_FACTOR_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CODE): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="one-time-code")
        ),
    }
)


class InternxtConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Internxt."""

    VERSION = 1

    def __init__(self) -> None:
        self._email: str | None = None
        self._password: str | None = None
        self._client: InternxtClient | None = None

    def _placeholders(self) -> dict[str, str]:
        return {"email": self._email or "", "upgrade_url": UPGRADE_URL}

    def _get_client(self) -> InternxtClient:
        if self._client is None:
            self._client = InternxtClient(
                async_get_clientsession(self.hass), client_name=CLIENT_NAME
            )
        return self._client

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for e-mail and password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip().lower()
            self._password = user_input[CONF_PASSWORD]
            await self.async_set_unique_id(None)  # set after login, from the account id
            result = await self._async_continue_login(errors)
            if result is not None:
                return result
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, {CONF_EMAIL: self._email} if self._email else {}
            ),
            description_placeholders=self._placeholders(),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication when the stored token stopped working."""
        self._email = entry_data[CONF_EMAIL]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            result = await self._async_continue_login(errors)
            if result is not None:
                return result
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_PASSWORD_SCHEMA,
            description_placeholders=self._placeholders(),
            errors=errors,
        )

    async def async_step_two_factor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the 2FA code."""
        errors: dict[str, str] = {}
        if user_input is not None:
            code = "".join(user_input[CONF_CODE].split())
            result = await self._async_login(code, errors)
            if result is not None:
                return result
            if errors.get("base") == "invalid_auth":
                # wrong password: go back to where the password is entered
                step = "reauth_confirm" if self.source == SOURCE_REAUTH else "user"
                return self.async_show_form(
                    step_id=step,
                    data_schema=STEP_PASSWORD_SCHEMA
                    if step == "reauth_confirm"
                    else STEP_USER_SCHEMA,
                    description_placeholders=self._placeholders(),
                    errors=errors,
                )
        return self.async_show_form(
            step_id="two_factor",
            data_schema=STEP_TWO_FACTOR_SCHEMA,
            description_placeholders=self._placeholders(),
            errors=errors,
        )

    async def _async_continue_login(
        self, errors: dict[str, str]
    ) -> ConfigFlowResult | None:
        """Check whether 2FA is needed before the first attempt (wrong codes count as failures)."""
        assert self._email is not None
        try:
            details = await self._get_client().security_details(self._email)
        except InternxtConnectionError:
            errors["base"] = "cannot_connect"
            return None
        except InternxtError:
            _LOGGER.exception("Unexpected error reading login details")
            errors["base"] = "unknown"
            return None
        if details.two_factor:
            return await self.async_step_two_factor()
        return await self._async_login(None, errors)

    async def _async_login(
        self, code: str | None, errors: dict[str, str]
    ) -> ConfigFlowResult | None:
        assert self._email is not None and self._password is not None
        client = self._get_client()
        try:
            credentials = await client.login(self._email, self._password, code)
        except InternxtTwoFactorRequired:
            errors["base"] = "invalid_2fa"
            return None
        except InternxtPlanError:
            errors["base"] = "plan_not_supported"
            return None
        except InternxtAccountBlocked:
            errors["base"] = "account_blocked"
            return None
        except InternxtAuthError:
            errors["base"] = "invalid_auth"
            return None
        except InternxtConnectionError:
            errors["base"] = "cannot_connect"
            return None
        except InternxtError:
            _LOGGER.exception("Unexpected error during login")
            errors["base"] = "unknown"
            return None
        self._password = None  # not needed anymore, never stored

        await self.async_set_unique_id(client.user.uuid)
        data = {CONF_TOKEN: credentials.token, CONF_MNEMONIC: credentials.mnemonic}
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates=data
            )
        self._abort_if_unique_id_configured()

        if not await self._async_preflight(client, errors):
            return None
        return self.async_create_entry(
            title=self._email,
            data={
                CONF_EMAIL: self._email,
                CONF_FOLDER_NAME: DEFAULT_FOLDER_NAME,
                **data,
            },
        )

    async def _async_preflight(
        self, client: InternxtClient, errors: dict[str, str]
    ) -> bool:
        """Create the backup folder and round-trip a small file, so problems show up now."""
        try:
            folder = await client.ensure_folder(
                client.user.root_folder_uuid, DEFAULT_FOLDER_NAME
            )
            probe = b"Home Assistant connection test for Internxt.\n"
            uploaded = await client.upload_bytes(
                probe, folder_uuid=folder.uuid, name=PREFLIGHT_FILE
            )
            try:
                if await client.download_bytes(uploaded) != probe:
                    raise InternxtError("connection test file came back different")
            finally:
                await client.delete_file(uploaded.uuid)
        except InternxtConnectionError:
            errors["base"] = "cannot_connect"
            return False
        except InternxtError:
            _LOGGER.exception("Connection test with Internxt failed")
            errors["base"] = "preflight_failed"
            return False
        return True
