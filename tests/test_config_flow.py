"""Tests for the Internxt config flow."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.internxt.aiointernxt import (
    InternxtAccountBlocked,
    InternxtConnectionError,
    InternxtPlanError,
)
from custom_components.internxt.const import (
    CONF_MNEMONIC,
    CONF_TOKEN,
    DEFAULT_FOLDER_NAME,
    DOMAIN,
)

from .conftest import EMAIL, MNEMONIC, PASSWORD, USER, FakeClient


@pytest.fixture(autouse=True)
def no_setup():
    with patch("custom_components.internxt.async_setup_entry", return_value=True):
        yield


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_user_flow_creates_entry_after_preflight(
    hass: HomeAssistant, fake_client: FakeClient
) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": " Simon@Example.com ", "password": PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == EMAIL
    assert result["data"] == {
        "email": EMAIL,
        "folder_name": DEFAULT_FOLDER_NAME,
        CONF_TOKEN: "token-login",
        CONF_MNEMONIC: MNEMONIC,
    }
    assert "password" not in result["data"]
    assert result["result"].unique_id == USER.uuid
    # preflight created the folder and removed its test file again
    assert [f.name for f in fake_client.folders.values()] == [DEFAULT_FOLDER_NAME]
    assert fake_client.files == {}
    assert len(fake_client.deleted) == 1


async def test_two_factor(hass: HomeAssistant, fake_client: FakeClient) -> None:
    fake_client.two_factor = True
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": PASSWORD}
    )
    assert result["step_id"] == "two_factor"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "000000"}
    )
    assert result["errors"] == {"base": "invalid_2fa"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123 456"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (InternxtPlanError("402"), "plan_not_supported"),
        (InternxtAccountBlocked("blocked"), "account_blocked"),
        (InternxtConnectionError("down"), "cannot_connect"),
    ],
)
async def test_login_errors(
    hass: HomeAssistant, fake_client: FakeClient, error: Exception, key: str
) -> None:
    fake_client.login_error = error
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": PASSWORD}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}
    fake_client.login_error = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_wrong_password(hass: HomeAssistant, fake_client: FakeClient) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": "nope"}
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_already_configured(
    hass: HomeAssistant, fake_client: FakeClient, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(
    hass: HomeAssistant, fake_client: FakeClient, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_TOKEN] == "token-login"
