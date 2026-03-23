"""Test powerllm config flow."""

from unittest.mock import patch

import pytest

from custom_components.telegram_bot_conversation.const import (
    CONF_ATTACHMENTS,
    CONF_CONVERSATION_TIMEOUT,
    CONF_DISABLE_WEB_PREV,
    CONF_LATEX,
    CONF_MERMAID,
    CONF_TELEGRAM_ENTRY,
    CONF_TELEGRAM_SUBENTRY,
    CONF_THOUGHTS,
    CONF_TMPDIR,
    CONF_USER,
    DOMAIN,
)
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant


@pytest.fixture(autouse=True)
def bypass_setup_fixture():
    """Prevent setup."""
    with patch(
        "custom_components.telegram_bot_conversation.async_setup_entry",
        return_value=True,
    ):
        yield


def test_test(hass):
    """Workaround for https://github.com/MatthewFlamm/pytest-homeassistant-custom-component/discussions/160."""


async def test_config_flow(hass: HomeAssistant, mock_telegram_config_entry) -> None:
    """Test a successful config flow."""
    # Init first step
    hass.config.allowlist_external_dirs |= {"/mnt/share/media"}
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert not result["errors"]
    assert result["step_id"] == "user"

    # Advance to step 2
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={},  # Use default values for the form
    )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert not result["errors"]
    assert result["step_id"] == "init"

    # Advance to step 3
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_TMPDIR: "/mnt/share/media"},
    )

    # Check that the config flow is complete and a new entry is created with
    # the input data
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "Mock Title"
    assert result["data"] == {CONF_TELEGRAM_ENTRY: mock_telegram_config_entry.entry_id}
    assert result["options"] == {CONF_TMPDIR: "/mnt/share/media"}
    assert result["result"]


async def test_options_flow(
    hass: HomeAssistant,
    mock_config_entry,
) -> None:
    """Test a successful options flow."""
    hass.config.allowlist_external_dirs |= {"/mnt/share/media"}
    options = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert options["type"] == data_entry_flow.FlowResultType.FORM
    assert options["step_id"] == "init"

    options = await hass.config_entries.options.async_configure(
        options["flow_id"],
        user_input={CONF_TMPDIR: "/mnt/share/media"},
    )
    assert options["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert options["data"] == {CONF_TMPDIR: "/mnt/share/media"}


async def test_subentry_reconfigure_flow(
    hass: HomeAssistant,
    mock_config_entry,
) -> None:
    """Test reconfiguring an existing telegram subentry."""
    subentry_id, subentry = next(iter(mock_config_entry.subentries.items()))

    result = await mock_config_entry.start_subentry_reconfigure_flow(hass, subentry_id)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert not result["errors"]
    assert result["step_id"] == "init"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            CONF_TELEGRAM_SUBENTRY: subentry.data[CONF_TELEGRAM_SUBENTRY],
            CONF_USER: subentry.data[CONF_USER],
            CONF_CONVERSATION_TIMEOUT: {"minutes": 30},
            CONF_ATTACHMENTS: 25,
            CONF_LATEX: True,
            CONF_MERMAID: True,
            CONF_DISABLE_WEB_PREV: False,
            CONF_THOUGHTS: True,
        },
    )

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    updated_subentry = hass.config_entries.async_get_known_entry(
        mock_config_entry.entry_id
    ).subentries[subentry_id]
    assert (
        updated_subentry.data[CONF_TELEGRAM_SUBENTRY]
        == subentry.data[CONF_TELEGRAM_SUBENTRY]
    )
    assert updated_subentry.data[CONF_USER] == subentry.data[CONF_USER]
    assert updated_subentry.data[CONF_CONVERSATION_TIMEOUT] == {"minutes": 30}
    assert updated_subentry.data[CONF_ATTACHMENTS] == 25
    assert updated_subentry.data[CONF_LATEX] is True
    assert updated_subentry.data[CONF_MERMAID] is True
    assert updated_subentry.data[CONF_DISABLE_WEB_PREV] is False
    assert updated_subentry.data[CONF_THOUGHTS] is True
