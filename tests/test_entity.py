"""Tests for telegram_bot_conversation entity."""

from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.telegram_bot_conversation.const import CONF_CONVERSATION_AGENT
from homeassistant.components import conversation
from homeassistant.components.telegram_bot.const import (
    ATTR_CHAT_ID,
    ATTR_MESSAGE_THREAD_ID,
)
from homeassistant.core import HomeAssistant


async def test_conversation_stream(
    hass: HomeAssistant,
    mock_receive_telegram_message: Callable[[str], Awaitable[None]],
    mock_conversation_agent: AsyncMock,
    mock_config_entry,
) -> None:
    """Test plain text conversation using a streaming agent."""
    mock_conversation_agent.return_value = [
        conversation.AssistantContentDeltaDict(
            {"role": "assistant", "content": "Hello!"}
        )
    ]
    events = async_capture_events(hass, "telegram_sent")

    await mock_receive_telegram_message("Hi!")

    assert len(events) == 1
    event = events[0]
    assert event.data[ATTR_CHAT_ID] == 12345678
    assert event.data[ATTR_MESSAGE_THREAD_ID] == 0
    assert event.context.user_id is not None

    mock_conversation_agent.assert_awaited_once()
    user_content = mock_conversation_agent.await_args.args[0]
    assert user_content.role == "user"
    assert user_content.content == "Hi!"

    chat_log = next(iter(hass.data.get(conversation.chat_log.DATA_CHAT_LOGS).values()))

    assert len(chat_log.content) == 3
    assert chat_log.content[1].content == "Hi!"
    assert chat_log.content[2].content == "Hello!"
    assert chat_log.content[2].agent_id == "Mock Agent ID"


async def test_conversation_nonstream(
    hass: HomeAssistant,
    mock_receive_telegram_message: Callable[[str], Awaitable[None]],
    mock_config_entry,
) -> None:
    """Test plain text conversation using a non-streaming agent."""
    subentry = next(iter(mock_config_entry.subentries.values()))
    hass.config_entries.async_update_subentry(
        mock_config_entry,
        subentry,
        data={k: v for k, v in subentry.data.items() if k != CONF_CONVERSATION_AGENT},
    )

    events = async_capture_events(hass, "telegram_sent")

    await mock_receive_telegram_message("Hi!")

    assert len(events) == 1
    event = events[0]
    assert event.data[ATTR_CHAT_ID] == 12345678
    assert event.data[ATTR_MESSAGE_THREAD_ID] == 0
    assert event.context.user_id is not None

    chat_log = next(iter(hass.data.get(conversation.chat_log.DATA_CHAT_LOGS).values()))

    assert len(chat_log.content) == 5
    assert chat_log.content[1].content == "Hi!"
    assert chat_log.content[-1].content == "Hello from Home Assistant."
    assert chat_log.content[-1].agent_id == "conversation.home_assistant"


async def test_prompt(
    hass: HomeAssistant,
    mock_receive_telegram_message: Callable[[str], Awaitable[None]],
    mock_conversation_agent: AsyncMock,
    mock_config_entry,
) -> None:
    """Test prompt."""
    mock_conversation_agent.return_value = [
        conversation.AssistantContentDeltaDict(
            {"role": "assistant", "content": "Hello!"}
        )
    ]
    await mock_receive_telegram_message("Hi!")

    chat_log = next(iter(hass.data.get(conversation.chat_log.DATA_CHAT_LOGS).values()))

    prompt = chat_log.content[0].content

    assert (
        "The user is interacting through Telegram. Markdown is fully supported."
        in prompt
    )
