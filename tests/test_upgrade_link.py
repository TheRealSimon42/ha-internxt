"""Plan error form carries the purchase link placeholder."""

from homeassistant import config_entries
from custom_components.internxt.aiointernxt import InternxtPlanError
from custom_components.internxt.const import DOMAIN
from tests.conftest import EMAIL, PASSWORD


async def test_plan_error_has_link(hass, fake_client):

    fake_client.login_error = InternxtPlanError("402")
    r = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert (
        r["description_placeholders"]["upgrade_url"] == "https://internxt.com/simon42"
    )
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"], {"email": EMAIL, "password": PASSWORD}
    )
    assert r["errors"] == {"base": "plan_not_supported"}
    assert (
        r["description_placeholders"]["upgrade_url"] == "https://internxt.com/simon42"
    )
