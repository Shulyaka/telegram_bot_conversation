"""Custom integration to integrate telegram_bot_conversation with Home Assistant.

This integration ties up the Home Assistant conversation
integration with the telegram_bot integration.

Requires telegram_bot to be set up.

Credits: https://gist.github.com/balloob/d59cae89d19a14bcec99ce1bde05bd44

For more details about this integration, please refer to
https://github.com/Shulyaka/telegram_bot_conversation
"""

from asyncio import CancelledError, Task
from collections.abc import Mapping
from contextlib import suppress
from types import MappingProxyType
from typing import Any

from homeassistant.components.conversation.chat_log import async_subscribe_chat_logs
from homeassistant.components.conversation.const import ChatLogEventType
from homeassistant.components.notify.const import DOMAIN as NOTIFY_DOMAIN
from homeassistant.components.telegram_bot.const import (
    ATTR_CHAT_ID,
    ATTR_USERNAME,
    CONF_CHAT_ID,
    CONF_CONFIG_ENTRY_ID,
    EVENT_TELEGRAM_ATTACHMENT,
    EVENT_TELEGRAM_CALLBACK,
    EVENT_TELEGRAM_COMMAND,
    EVENT_TELEGRAM_TEXT,
    SUBENTRY_TYPE_ALLOWED_CHAT_IDS,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from .const import (
    CONF_TELEGRAM_ENTRY,
    CONF_TELEGRAM_SUBENTRY,
    CONF_TMPDIR,
    CONF_USER,
    CONF_WEB_PREVIEW,
    DOMAIN,
    LOGGER,
    WebPreview,
)
from .entity import TelegramChatHandler
from .recursive_data_flow import validate_data, validate_options, validate_subentry_data

type TelegramBotConversationConfigEntry = ConfigEntry[TelegramBotConversationHandler]


class TelegramBotConversationHandler:
    """Handle Telegram bot conversation events."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: TelegramBotConversationConfigEntry,
        data: MappingProxyType[str, Any],
        options: MappingProxyType[str, Any],
        subentries_data: dict[str, MappingProxyType[str, Any]],
    ) -> None:
        """Initialize the handler."""
        self.hass = hass
        self.entry = entry
        self.telegram_entry_id = data[CONF_TELEGRAM_ENTRY]

        telegram_entry = hass.config_entries.async_get_entry(self.telegram_entry_id)
        if not telegram_entry:
            # Create an issue so the user gets actionable guidance instead of a reload loop
            ir.async_create_issue(
                hass,
                DOMAIN,
                "all_telegram_entries_configured",
                translation_key="all_telegram_entries_configured",
                severity=ir.IssueSeverity.ERROR,
                is_fixable=False,
            )
            raise ConfigEntryNotReady("Telegram entry not found")

        linked_telegram_subentries = {
            data[CONF_TELEGRAM_SUBENTRY] for data in subentries_data.values()
        }

        telegram_id_map = {
            subentry_id: int(subentry.data[CONF_CHAT_ID])
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

        user_id_map: dict[int, str] = {
            telegram_id_map[data[CONF_TELEGRAM_SUBENTRY]]: str(data.get(CONF_USER))
            for data in subentries_data.values()
            if data.get(CONF_USER) is not None
            and data.get(CONF_TELEGRAM_SUBENTRY) in telegram_id_map
        }

        self.chat_handlers: dict[int, TelegramChatHandler] = {}
        for subentry_id, subentry_data in subentries_data.items():
            tg_sub = subentry_data[CONF_TELEGRAM_SUBENTRY]
            if tg_sub not in telegram_id_map:
                continue
            chat_id = telegram_id_map[tg_sub]
            self.chat_handlers[chat_id] = TelegramChatHandler(
                hass=hass,
                entry=entry,
                chat_id=chat_id,
                notify_entity_id=telegram_notify_map.get(tg_sub),
                subentry_id=subentry_id,
                user_id_map=user_id_map,
                config=data | options | subentry_data,
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

        self.entry.async_on_unload(
            async_subscribe_chat_logs(
                self.hass,
                self._handle_chat_log_event,
            )
        )

    @callback
    def _handle_chat_log_event(
        self, conversation_id: str, event_type: ChatLogEventType, data: dict[str, Any]
    ) -> None:
        """Handle conversation chat log events."""
        parts = conversation_id.split("_")
        if len(parts) < 2 or parts[0] != "telegram":
            return
        try:
            chat_id = int(parts[1])
            if len(parts) == 3:
                thread_id = int(parts[2])
            else:
                thread_id = 0
        except ValueError:
            LOGGER.warning("Invalid conversation_id format: %s", conversation_id)
            return

        handler = self.chat_handlers.get(chat_id)
        if handler is None:
            LOGGER.debug(
                "No chat handler found for chat_id=%s, thread_id=%s; ignoring chat log event",
                chat_id,
                thread_id,
            )
            return

        def log_exceptions(task: Task[None]) -> None:
            """Log exceptions from async_handle_chat_log_event."""
            with suppress(CancelledError):
                if err := task.exception():
                    LOGGER.error(
                        "Error in async_handle_chat_log_event for chat_id=%s, thread_id=%s: %s",
                        chat_id,
                        thread_id,
                        err,
                        exc_info=err,
                    )

        self.entry.async_create_task(
            self.hass,
            handler.async_handle_chat_log_event(thread_id, event_type, data, Context()),
            "async_handle_chat_log_event",
        ).add_done_callback(log_exceptions)

    async def async_handle_text(self, event: Event) -> None:
        """Handle text and attachment events."""
        try:
            await self.chat_handlers[event.data[ATTR_CHAT_ID]].async_handle_text(event)
        except Exception as e:  # noqa: BLE001
            LOGGER.exception("Error handling text/attachment event: %s", e)

    @callback
    def text_events_filter(self, event_data: Mapping[str, Any]) -> bool:
        """Filter text and attachment events."""
        return (
            event_data.get("bot", {}).get(CONF_CONFIG_ENTRY_ID)
            == self.telegram_entry_id
            and event_data.get(ATTR_CHAT_ID) in self.chat_handlers
        )

    async def async_handle_command(self, event: Event) -> None:
        """Handle command events."""
        try:
            await self.chat_handlers[event.data[ATTR_CHAT_ID]].async_handle_command(
                event
            )
        except Exception as e:  # noqa: BLE001
            LOGGER.exception("Error handling command event: %s", e)

    @callback
    def command_events_filter(self, event_data: Mapping[str, Any]) -> bool:
        """Filter command events."""
        command = event_data.get("command", "")
        command_parts = command.split("@")
        if len(command_parts) == 2:
            if command_parts[1] != event_data.get("bot", {}).get(ATTR_USERNAME):
                return False
            command = command_parts[0]
        return (
            command in ("/model", "/new")
            and event_data.get("bot", {}).get(CONF_CONFIG_ENTRY_ID)
            == self.telegram_entry_id
            and event_data.get(ATTR_CHAT_ID) in self.chat_handlers
        )

    async def async_handle_callback(self, event: Event) -> None:
        """Handle callback query events."""
        try:
            await self.chat_handlers[event.data[ATTR_CHAT_ID]].async_handle_callback(
                event
            )
        except Exception as e:  # noqa: BLE001
            LOGGER.exception("Error handling callback event: %s", e)

    @callback
    def callback_events_filter(self, event_data: Mapping[str, Any]) -> bool:
        """Filter callback query events."""
        return (
            event_data.get("data", "").startswith(("/model", "/new"))
            and event_data.get("bot", {}).get(CONF_CONFIG_ENTRY_ID)
            == self.telegram_entry_id
            and event_data.get(ATTR_CHAT_ID) in self.chat_handlers
        )

    async def handle_generate_image_intent(
        self, event: Event, context: Context, prompt: str
    ) -> str:
        """Handle the generate image intent."""
        chat_id = event.data.get(ATTR_CHAT_ID)
        if chat_id is None:
            raise HomeAssistantError(
                "Missing chat_id in event data for generate image intent"
            )

        chat_handler = self.chat_handlers.get(chat_id)
        if chat_handler is None:
            raise HomeAssistantError(
                f"Chat ID {chat_id} is not configured for this integration"
            )

        return await chat_handler.handle_generate_image_intent(event, context, prompt)


async def async_setup_entry(
    hass: HomeAssistant, entry: TelegramBotConversationConfigEntry
) -> bool:
    """Set up this integration using UI."""

    try:
        data = await validate_data(hass, entry)
        options = await validate_options(hass, entry)
        subentries_data = {
            subentry_id: await validate_subentry_data(hass, entry, subentry_id)
            for subentry_id, subentry in entry.subentries.items()
            if subentry.subentry_type == "telegram_id"
        }
    except HomeAssistantError as e:
        LOGGER.error("Configuration validation error: %s", e)

        if str(e) in ("no_telegram_bot_entries", "all_telegram_entries_configured"):
            ir.async_create_issue(
                hass,
                DOMAIN,
                str(e),
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=str(e),
            )

        raise ConfigEntryNotReady(f"Configuration error: {e}") from e

    entry.runtime_data = TelegramBotConversationHandler(
        hass, entry, data=data, options=options, subentries_data=subentries_data
    )

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


async def async_migrate_entry(
    hass: HomeAssistant, entry: TelegramBotConversationConfigEntry
) -> bool:
    """Migrate entry."""
    LOGGER.debug("Migrating from version %s:%s", entry.version, entry.minor_version)

    if entry.version > 1:
        # This means the user has downgraded from a future version
        return False

    if entry.version == 1 and entry.minor_version == 1:
        # Move customization options to subentries
        options = entry.options.copy()
        hass.config_entries.async_update_entry(
            entry,
            options={CONF_TMPDIR: options.pop(CONF_TMPDIR, hass.config.path("www"))},
            minor_version=2,
        )
        for subentry in entry.subentries.values():
            data = subentry.data.copy()
            data.update(options)
            hass.config_entries.async_update_subentry(entry, subentry, data=data)

    if entry.version == 1 and entry.minor_version == 2:
        # Migrate disable_web_page_preview option
        CONF_DISABLE_WEB_PREV = "disable_web_page_preview"

        for subentry in entry.subentries.values():
            data = subentry.data.copy()
            disable_web_page_preview = data.get(CONF_DISABLE_WEB_PREV)
            if disable_web_page_preview is None:
                continue
            data[CONF_WEB_PREVIEW] = (
                WebPreview.OFF.value
                if disable_web_page_preview
                else WebPreview.ON.value
            )
            hass.config_entries.async_update_subentry(entry, subentry, data=data)

        hass.config_entries.async_update_entry(entry, minor_version=3)

    LOGGER.debug(
        "Migration to version %s:%s successful", entry.version, entry.minor_version
    )

    return True
