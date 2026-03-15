"""Tests for telegram_bot_conversation runtime setup."""

from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.telegram_bot_conversation import TelegramBotConversationHandler
from custom_components.telegram_bot_conversation.const import CONF_TELEGRAM_SUBENTRY
from homeassistant.components.telegram_bot.const import (
    ATTR_CHAT_ID,
    ATTR_MESSAGE,
    ATTR_MESSAGE_THREAD_ID,
    CONF_CHAT_ID,
    CONF_CONFIG_ENTRY_ID,
    DOMAIN as TELEGRAM_DOMAIN,
    SERVICE_SEND_MESSAGE,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er


async def test_handler_resolves_notify_entity_id(
    hass: HomeAssistant,
    mock_telegram_config_entry,
    mock_config_entry,
) -> None:
    """Test that chat handler stores the telegram notify entity ID."""
    handler = TelegramBotConversationHandler(hass, mock_config_entry)
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
    handler = TelegramBotConversationHandler(hass, mock_config_entry)

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
        message_thread_id=0,
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
