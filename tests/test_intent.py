"""Tests for telegram_bot_conversation intents."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.telegram_bot_conversation.const import DOMAIN
from custom_components.telegram_bot_conversation.intent import INTENT_GENERATE_IMAGE
from homeassistant.components.telegram_bot.const import (
    ATTR_CHAT_ID,
    ATTR_MESSAGE_THREAD_ID,
    CONF_CONFIG_ENTRY_ID,
    EVENT_TELEGRAM_TEXT,
)
from homeassistant.core import Context, Event, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import intent


def _telegram_context(
    telegram_entry_id: str,
    *,
    chat_id: int = 12345678,
    event_type: str = EVENT_TELEGRAM_TEXT,
) -> Context:
    """Create a context originating from a Telegram event."""
    context = Context()
    context.origin_event = Event(
        event_type,
        {
            ATTR_CHAT_ID: chat_id,
            ATTR_MESSAGE_THREAD_ID: 0,
            "bot": {CONF_CONFIG_ENTRY_ID: telegram_entry_id},
        },
    )
    return context


async def test_generate_image_intent_calls_runtime_handler(
    hass: HomeAssistant,
    mock_config_entry,
    mock_telegram_config_entry,
) -> None:
    """Test the image intent dispatches to the runtime handler."""
    runtime_handler = SimpleNamespace(
        handle_generate_image_intent=AsyncMock(return_value="Image sent")
    )
    mock_config_entry.runtime_data = runtime_handler

    context = _telegram_context(mock_telegram_config_entry.entry_id)

    response = await intent.async_handle(
        hass,
        DOMAIN,
        INTENT_GENERATE_IMAGE,
        slots={"prompt": {"value": "Draw a lighthouse at sunset"}},
        context=context,
    )

    runtime_handler.handle_generate_image_intent.assert_awaited_once_with(
        context.origin_event,
        context,
        prompt="Draw a lighthouse at sunset",
    )
    assert response.error_code is None
    assert response.speech["plain"]["speech"] == "Image sent"


async def test_generate_image_intent_requires_prompt_slot(
    hass: HomeAssistant,
    mock_init_component,
) -> None:
    """Test the image intent requires a prompt slot."""
    with pytest.raises(intent.InvalidSlotInfo):
        await intent.async_handle(
            hass,
            DOMAIN,
            INTENT_GENERATE_IMAGE,
            slots={},
            context=Context(),
        )


async def test_generate_image_intent_non_telegram_context(
    hass: HomeAssistant,
    mock_config_entry,
    mock_telegram_config_entry,
) -> None:
    """Test the image intent is handled outside Telegram event contexts in certain cases."""
    runtime_handler = SimpleNamespace(
        handle_generate_image_intent=AsyncMock(return_value="Image sent")
    )
    mock_config_entry.runtime_data = runtime_handler

    response = await intent.async_handle(
        hass,
        DOMAIN,
        INTENT_GENERATE_IMAGE,
        slots={"prompt": {"value": "Draw a fox"}},
        context=Context(),
    )

    context = _telegram_context(mock_telegram_config_entry.entry_id)
    runtime_handler.handle_generate_image_intent.assert_awaited_once()
    (call_event, call_context), call_kwargs = (
        runtime_handler.handle_generate_image_intent.await_args
    )
    assert call_kwargs == {"prompt": "Draw a fox"}
    assert call_event.event_type == context.origin_event.event_type
    assert call_event.data == context.origin_event.data
    assert call_context.user_id == next(
        iter(mock_config_entry.subentries.values())
    ).data.get("user_id")

    assert response.error_code is None
    assert response.speech["plain"]["speech"] == "Image sent"


async def test_generate_image_intent_returns_handler_error(
    hass: HomeAssistant,
    mock_config_entry,
    mock_telegram_config_entry,
) -> None:
    """Test the image intent returns Home Assistant handler errors to the caller."""
    runtime_handler = SimpleNamespace(
        handle_generate_image_intent=AsyncMock(
            side_effect=HomeAssistantError("Image generation failed")
        )
    )
    mock_config_entry.runtime_data = runtime_handler

    response = await intent.async_handle(
        hass,
        DOMAIN,
        INTENT_GENERATE_IMAGE,
        slots={"prompt": {"value": "Draw a fox"}},
        context=_telegram_context(mock_telegram_config_entry.entry_id),
    )

    assert response.error_code == intent.IntentResponseErrorCode.FAILED_TO_HANDLE
    assert response.speech["plain"]["speech"] == "Image generation failed"
