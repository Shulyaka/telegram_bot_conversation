"""Tests for telegram_bot_conversation runtime setup."""

from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.telegram_bot_conversation import TelegramBotConversationHandler
from custom_components.telegram_bot_conversation.const import CONF_TELEGRAM_SUBENTRY
from custom_components.telegram_bot_conversation.recursive_data_flow import (
    validate_data,
    validate_options,
    validate_subentry_data,
)
from homeassistant.components.conversation import (
    AssistantContent,
    UserContent,
    async_get_chat_log,
)
from homeassistant.components.conversation.chat_log import DATA_CHAT_LOGS
from homeassistant.components.telegram_bot.const import (
    ATTR_CHAT_ID,
    ATTR_MESSAGE,
    ATTR_MESSAGE_THREAD_ID,
    CONF_CHAT_ID,
    CONF_CONFIG_ENTRY_ID,
    DOMAIN as TELEGRAM_DOMAIN,
    SERVICE_SEND_MESSAGE,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.chat_session import DATA_CHAT_SESSION, async_get_chat_session


async def _get_config(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> tuple[dict, dict, dict]:
    """Extract the main config, subentry data, and options from a config entry."""
    data = await validate_data(hass, config_entry)
    options = await validate_options(hass, config_entry)
    subentry_data = {
        subentry_id: await validate_subentry_data(hass, config_entry, subentry_id)
        for subentry_id, subentry in config_entry.subentries.items()
        if subentry.subentry_type == "telegram_id"
    }
    return data, options, subentry_data


async def test_handler_resolves_notify_entity_id(
    hass: HomeAssistant,
    mock_telegram_config_entry,
    mock_config_entry,
) -> None:
    """Test that chat handler stores the telegram notify entity ID."""
    handler = TelegramBotConversationHandler(
        hass, mock_config_entry, *await _get_config(hass, mock_config_entry)
    )
    entity_registry = er.async_get(hass)

    telegram_subentry_id, telegram_subentry = next(
        iter(mock_telegram_config_entry.subentries.items())
    )
    notify_entity_id = next(
        entity_entry.entity_id
        for entity_entry in er.async_entries_for_config_entry(
            entity_registry, mock_telegram_config_entry.entry_id
        )
        if entity_entry.config_subentry_id == telegram_subentry_id
        and entity_entry.entity_id.startswith("notify.")
    )

    chat_handler = handler.chat_handlers[telegram_subentry.data[CONF_CHAT_ID]]

    assert chat_handler.notify_entity_id == notify_entity_id
    assert chat_handler.subentry_id == next(
        subentry.subentry_id
        for subentry in mock_config_entry.subentries.values()
        if subentry.data[CONF_TELEGRAM_SUBENTRY] == telegram_subentry_id
    )


async def test_send_message_uses_notify_entity_id(
    hass: HomeAssistant,
    mock_telegram_config_entry,
    mock_config_entry,
) -> None:
    """Test that send_message targets the telegram notify entity when available."""
    handler = TelegramBotConversationHandler(
        hass, mock_config_entry, *await _get_config(hass, mock_config_entry)
    )

    # Pick the first chat to test with
    _, telegram_subentry = next(iter(mock_telegram_config_entry.subentries.items()))
    chat_id = telegram_subentry.data[CONF_CHAT_ID]
    chat_handler = handler.chat_handlers[chat_id]

    calls = async_mock_service(
        hass,
        TELEGRAM_DOMAIN,
        SERVICE_SEND_MESSAGE,
        response={"chats": []},
    )

    await chat_handler.send_message(
        message="Hello",
        thread_id=0,
    )

    assert len(calls) == 1
    call = calls[0]
    assert call.domain == TELEGRAM_DOMAIN
    assert call.service == SERVICE_SEND_MESSAGE
    assert call.data[ATTR_MESSAGE] == "Hello"
    assert call.data[ATTR_ENTITY_ID] == [chat_handler.notify_entity_id]
    assert ATTR_CHAT_ID not in call.data
    assert call.data[ATTR_MESSAGE_THREAD_ID] == 0
    assert call.data[CONF_CONFIG_ENTRY_ID] == mock_telegram_config_entry.entry_id


async def test_new_command_clears_history_immediately(
    hass: HomeAssistant,
    mock_telegram_config_entry,
    mock_config_entry,
) -> None:
    """Test that the /new command drops the stored session and chat log right away."""
    handler = TelegramBotConversationHandler(
        hass, mock_config_entry, *await _get_config(hass, mock_config_entry)
    )

    _, telegram_subentry = next(iter(mock_telegram_config_entry.subentries.items()))
    chat_id = telegram_subentry.data[CONF_CHAT_ID]
    chat_handler = handler.chat_handlers[chat_id]
    conversation_id = f"telegram_{chat_id}"

    with (
        async_get_chat_session(hass, conversation_id) as session,
        async_get_chat_log(hass, session) as chat_log,
    ):
        chat_log.async_add_user_content(UserContent(content="Hello"))
        chat_log.async_add_assistant_content_without_tools(
            AssistantContent(agent_id="test-agent", content="Hi there")
        )

    assert conversation_id in hass.data[DATA_CHAT_SESSION]
    assert conversation_id in hass.data[DATA_CHAT_LOGS]

    context = Context()
    with patch.object(chat_handler, "send_message", AsyncMock()) as send_message:
        await chat_handler.async_process_command(0, "/new", [], context)

    assert conversation_id not in hass.data[DATA_CHAT_SESSION]
    assert conversation_id not in hass.data[DATA_CHAT_LOGS]
    send_message.assert_awaited_once_with(
        message="New conversation started.",
        thread_id=0,
        context=context,
    )

    with (
        async_get_chat_session(hass, conversation_id) as session,
        async_get_chat_log(hass, session) as chat_log,
    ):
        assert [content.role for content in chat_log.content] == ["system"]
