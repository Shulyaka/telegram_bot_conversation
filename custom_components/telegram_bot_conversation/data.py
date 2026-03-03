"""Custom types for telegram_bot_conversation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.loader import Integration

    from .api import TelegramBotConversationApiClient
    from .coordinator import TelegramBotConversationDataUpdateCoordinator


type TelegramBotConversationConfigEntry = ConfigEntry[TelegramBotConversationData]


@dataclass
class TelegramBotConversationData:
    """Data for the Telegram Bot Conversation integration."""

    client: TelegramBotConversationApiClient
    coordinator: TelegramBotConversationDataUpdateCoordinator
    integration: Integration
