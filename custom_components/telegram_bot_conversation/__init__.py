"""Custom integration to integrate telegram_bot_conversation with Home Assistant.

This integration ties up the Home Assistant conversation
integration with the telegram_bot integration.

Requires telegram_bot to be set up.

Credits: https://gist.github.com/balloob/d59cae89d19a14bcec99ce1bde05bd44

For more details about this integration, please refer to
https://github.com/Shulyaka/telegram_bot_conversation
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.notify.const import DOMAIN as NOTIFY_DOMAIN
from homeassistant.components.telegram_bot.const import (
    ATTR_CHAT_ID,
    ATTR_USER_ID,
    CONF_CHAT_ID,
    CONF_CONFIG_ENTRY_ID,
    EVENT_TELEGRAM_ATTACHMENT,
    EVENT_TELEGRAM_CALLBACK,
    EVENT_TELEGRAM_COMMAND,
    EVENT_TELEGRAM_TEXT,
    SUBENTRY_TYPE_ALLOWED_CHAT_IDS,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_CONVERSATION_AGENT,
    CONF_TELEGRAM_ENTRY,
    CONF_TELEGRAM_SUBENTRY,
    CONF_USER,
)
from .entity import TelegramChatHandler

type TelegramBotConversationConfigEntry = ConfigEntry[None]


class TelegramBotConversationHandler:
    """Handle Telegram bot conversation events."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: TelegramBotConversationConfigEntry,
    ) -> None:
        """Initialize the handler."""
        self.hass = hass
        self.entry = entry
        self.telegram_entry_id = entry.data[CONF_TELEGRAM_ENTRY]

        telegram_entry = hass.config_entries.async_get_entry(self.telegram_entry_id)
        if not telegram_entry:
            raise ConfigEntryNotReady("Telegram entry not found")

        # TODO(@Shulyaka): Check if a telegram subentry has been deleted and raise
        # a repair issue

        linked_telegram_subentries = {
            s.data[CONF_TELEGRAM_SUBENTRY]
            for s in entry.subentries.values()
            if s.subentry_type == "telegram_id"
        }

        telegram_id_map = {
            subentry_id: subentry.data[CONF_CHAT_ID]
            for subentry_id, subentry in telegram_entry.subentries.items()
            if subentry.subentry_type == SUBENTRY_TYPE_ALLOWED_CHAT_IDS
            and subentry.data.get(CONF_CHAT_ID) is not None
            and subentry.subentry_id in linked_telegram_subentries
        }
        entity_registry = er.async_get(hass)
        telegram_notify_map = {
            entity_entry.config_subentry_id: entity_entry.entity_id
            for entity_entry in er.async_entries_for_config_entry(
                entity_registry, telegram_entry.entry_id
            )
            if entity_entry.config_subentry_id in linked_telegram_subentries
            and entity_entry.domain == NOTIFY_DOMAIN
        }

        self.chat_handlers: dict[int, TelegramChatHandler] = {}
        for subentry_id, subentry in entry.subentries.items():
            if subentry.subentry_type != "telegram_id":
                continue
            tg_sub = subentry.data.get(CONF_TELEGRAM_SUBENTRY)
            if tg_sub not in telegram_id_map:
                continue
            chat_id = telegram_id_map[tg_sub]
            self.chat_handlers[chat_id] = TelegramChatHandler(
                hass=hass,
                entry=entry,
                chat_id=chat_id,
                telegram_entry_id=self.telegram_entry_id,
                user_id=subentry.data.get(CONF_USER),
                agent_id=subentry.data.get(CONF_CONVERSATION_AGENT),
                notify_entity_id=telegram_notify_map.get(tg_sub),
                subentry_id=subentry_id,
            )

        self._register_listeners()

    def _register_listeners(self) -> None:
        """Register all event listeners."""
        self.entry.async_on_unload(
            self.hass.bus.async_listen(
                EVENT_TELEGRAM_TEXT,
                self.async_handle_text,
                self.text_events_filter,
            )
        )

        self.entry.async_on_unload(
            self.hass.bus.async_listen(
                EVENT_TELEGRAM_ATTACHMENT,
                self.async_handle_text,
                self.text_events_filter,
            )
        )

        self.entry.async_on_unload(
            self.hass.bus.async_listen(
                EVENT_TELEGRAM_COMMAND,
                self.async_handle_command,
                self.command_events_filter,
            )
        )

        self.entry.async_on_unload(
            self.hass.bus.async_listen(
                EVENT_TELEGRAM_CALLBACK,
                self.async_handle_callback,
                self.callback_events_filter,
            )
        )

    async def async_handle_text(self, event: Event) -> None:
        """Handle text and attachment events."""
        await self.chat_handlers[event.data[ATTR_CHAT_ID]].async_handle_text(event)

    @callback
    def text_events_filter(self, event_data: Mapping[str, Any]) -> bool:
        """Filter text and attachment events."""
        return (
            event_data.get("bot", {}).get(CONF_CONFIG_ENTRY_ID)
            == self.telegram_entry_id
            and event_data.get(ATTR_CHAT_ID) in self.chat_handlers
            and event_data.get(ATTR_CHAT_ID) == event_data.get(ATTR_USER_ID)
        )

    async def async_handle_command(self, event: Event) -> None:
        """Handle command events."""
        await self.chat_handlers[event.data[ATTR_CHAT_ID]].async_handle_command(event)

    @callback
    def command_events_filter(self, event_data: Mapping[str, Any]) -> bool:
        """Filter command events."""
        return (
            event_data.get("command") in ["/model", "/new"]
            and event_data.get("bot", {}).get(CONF_CONFIG_ENTRY_ID)
            == self.telegram_entry_id
            and event_data.get(ATTR_CHAT_ID) in self.chat_handlers
            and event_data.get(ATTR_CHAT_ID) == event_data.get(ATTR_USER_ID)
        )

    async def async_handle_callback(self, event: Event) -> None:
        """Handle callback query events."""
        await self.chat_handlers[event.data[ATTR_CHAT_ID]].async_handle_callback(event)

    @callback
    def callback_events_filter(self, event_data: Mapping[str, Any]) -> bool:
        """Filter callback query events."""
        return (
            event_data.get("data", "").startswith(("/model", "/new"))
            and event_data.get("bot", {}).get(CONF_CONFIG_ENTRY_ID)
            == self.telegram_entry_id
            and event_data.get(ATTR_CHAT_ID) in self.chat_handlers
            and event_data.get(ATTR_CHAT_ID) == event_data.get(ATTR_USER_ID)
        )


async def async_setup_entry(
    hass: HomeAssistant, entry: TelegramBotConversationConfigEntry
) -> bool:
    """Set up this integration using UI."""
    TelegramBotConversationHandler(hass, entry)

    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: TelegramBotConversationConfigEntry
) -> bool:
    """Unload Telegram Bot Conversation."""
    return True


async def async_update_options(
    hass: HomeAssistant, entry: TelegramBotConversationConfigEntry
) -> None:
    """Update options."""
    await hass.config_entries.async_reload(entry.entry_id)
