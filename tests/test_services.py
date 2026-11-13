"""Tests for integration services."""

from custom_components.telegram_bot_conversation import SERVICE_MARKDOWNIFY
from custom_components.telegram_bot_conversation.const import DOMAIN
from homeassistant.components.telegram_bot.const import (
    TelegramBotParseMode,
    TelegramBotServiceArgument,
)
from homeassistant.core import HomeAssistant


async def test_markdownify_service_strips_markdown(
    hass: HomeAssistant,
    mock_init_component,
) -> None:
    """Test the markdownify service can return plain text."""
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_MARKDOWNIFY,
        {
            "text": "Hello, **world**!",
            TelegramBotServiceArgument.PARSE_MODE: TelegramBotParseMode.PLAIN_TEXT,
        },
        blocking=True,
        return_response=True,
    )

    assert response == {"messages": ["Hello, world!"]}


async def test_markdownify_service_with_markdownv2(
    hass: HomeAssistant,
    mock_init_component,
) -> None:
    """Test the markdownify service preserves link entities in MarkdownV2."""
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_MARKDOWNIFY,
        {
            "text": "**Read** [Home Assistant](https://www.home-assistant.io/) ![in 2 hours](tg://time?unix=1780000000&format=r)",
            TelegramBotServiceArgument.PARSE_MODE: TelegramBotParseMode.MARKDOWN_V2,
        },
        blocking=True,
        return_response=True,
    )

    assert response == {
        "messages": [
            "*Read* [Home Assistant](https://www.home-assistant.io/) ![in 2 hours](tg://time?unix=1780000000&format=r)"
        ]
    }
