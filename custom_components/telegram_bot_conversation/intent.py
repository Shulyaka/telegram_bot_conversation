"""Intents for the Telegram Bot Conversation integration."""

from typing import Any

import voluptuous as vol

from homeassistant.components.telegram_bot.const import (
    ATTR_CHAT_ID,
    ATTR_MESSAGE_THREAD_ID,
    CONF_CHAT_ID,
    CONF_CONFIG_ENTRY_ID,
    EVENT_TELEGRAM_ATTACHMENT,
    EVENT_TELEGRAM_TEXT,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import intent

from .const import (
    CONF_TELEGRAM_ENTRY,
    CONF_TELEGRAM_SUBENTRY,
    CONF_USER,
    DOMAIN,
    LOGGER,
)

INTENT_GENERATE_IMAGE = "TelegramGenerateImage"


async def async_setup_intents(hass: HomeAssistant) -> None:
    """Set up the telegram intents."""
    intent.async_register(hass, GenerateImageHandler())


class BaseTelegramBotConversationIntentHandler(intent.IntentHandler):
    """Base class for Telegram Bot Conversation intent handlers."""

    platforms = None
    method: str

    @callback
    def async_can_handle(self, intent_obj: intent.Intent) -> bool:
        """Test if an intent can be handled."""
        return bool(
            super().async_can_handle(intent_obj)
            and (context := intent_obj.context)
            and (event := context.origin_event)
            and event.event_type in (EVENT_TELEGRAM_TEXT, EVENT_TELEGRAM_ATTACHMENT)
        )

    async def _get_event(
        self, intent_obj: intent.Intent
    ) -> tuple[Context, Event] | None:
        """Extract the Telegram event from the intent."""
        hass = intent_obj.hass

        if not (context := intent_obj.context):
            context = Context()

        if not (event := context.origin_event) or event.event_type not in (
            EVENT_TELEGRAM_TEXT,
            EVENT_TELEGRAM_ATTACHMENT,
        ):
            user_id = context.user_id

            LOGGER.debug(
                "Not called from Telegram Bot Conversation, trying to find the right "
                "chat for the user by context"
            )
            # Trying to be smart and get the right chat even if no event is available
            user_id_map: dict[str | None, list[tuple[str, Any]]] = {}
            user_id_private_chat_map: dict[str | None, list[tuple[str, Any]]] = {}
            for entry in hass.config_entries.async_entries(DOMAIN):
                if entry.state != ConfigEntryState.LOADED:
                    continue
                if not (
                    telegram_entry_id := entry.data.get(CONF_TELEGRAM_ENTRY)
                ) or not (
                    telegram_entry := hass.config_entries.async_get_entry(
                        telegram_entry_id
                    )
                ):
                    continue
                for subentry in entry.subentries.values():
                    subentry_user_id = subentry.data.get(CONF_USER)
                    if not (
                        telegram_subentry_id := subentry.data.get(
                            CONF_TELEGRAM_SUBENTRY
                        )
                    ) or not (
                        telegram_subentry := telegram_entry.subentries.get(
                            telegram_subentry_id
                        )
                    ):
                        continue
                    user_id_map.setdefault(subentry_user_id, []).append(
                        (telegram_entry_id, telegram_subentry.data.get(CONF_CHAT_ID))
                    )
                    if telegram_subentry.data.get(CONF_CHAT_ID, 0) > 0:
                        user_id_private_chat_map.setdefault(
                            subentry_user_id, []
                        ).append(
                            (
                                telegram_entry_id,
                                telegram_subentry.data.get(CONF_CHAT_ID),
                            )
                        )

            if len(user_id_map) == 1 and (
                (configured_user_id := next(iter(user_id_map.keys()))) is None
                or user_id is None
            ):
                LOGGER.debug("No user id, but single chat configured")
                user_id = configured_user_id
            if len(user_id_private_chat_map) == 1 and (
                (configured_user_id := next(iter(user_id_private_chat_map.keys())))
                is None
                or user_id is None
            ):
                LOGGER.debug("No user id, but single private chat configured")
                user_id = configured_user_id

            if user_id not in user_id_map:
                LOGGER.debug("No chat found for the user")
                return None

            if len(user_id_map.get(user_id, [])) == 1:
                LOGGER.debug("Single chat configured for this user")
                telegram_entry_id, chat_id = user_id_map[user_id][0]
            elif len(user_id_private_chat_map.get(user_id, [])) == 1:
                LOGGER.debug(
                    "Multiple chats, but single private chat configured for this user,"
                    " prefer private chat"
                )
                telegram_entry_id, chat_id = user_id_private_chat_map[user_id][0]
            else:
                LOGGER.debug(
                    "Not called from Telegram Bot Conversation and couldn't determine "
                    "a single chat for the user"
                )
                return None

            if not context.user_id and user_id:
                LOGGER.debug("Setting context user id to %s", user_id)
                context.user_id = user_id

            event_data: dict[str, Any] = {
                ATTR_CHAT_ID: chat_id,
                ATTR_MESSAGE_THREAD_ID: 0,
                "bot": {
                    CONF_CONFIG_ENTRY_ID: telegram_entry_id,
                },
            }

            event = Event(EVENT_TELEGRAM_TEXT, event_data, context=context)

        return context, event

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Handle the intent."""
        hass = intent_obj.hass
        slots = {
            k: v["value"]
            for k, v in self.async_validate_slots(intent_obj.slots).items()
        }

        if (result := await self._get_event(intent_obj)) is not None:
            context, event = result
        else:
            response = intent_obj.create_response()
            response.async_set_error(
                intent.IntentResponseErrorCode.FAILED_TO_HANDLE,
                "Cannot determine the Telegram chat from context.",
            )
            return response

        if not (
            telegram_entry_id := event.data.get("bot", {}).get(CONF_CONFIG_ENTRY_ID)
        ):
            response = intent_obj.create_response()
            response.async_set_error(
                intent.IntentResponseErrorCode.FAILED_TO_HANDLE,
                "Could not determine the telegram bot associated with the event.",
            )
            return response

        config_entry = next(
            (
                entry
                for entry in hass.config_entries.async_entries(DOMAIN)
                if entry.state == ConfigEntryState.LOADED
                and entry.data[CONF_TELEGRAM_ENTRY] == telegram_entry_id
            ),
            None,
        )

        if not config_entry or not (conversation_handler := config_entry.runtime_data):
            response = intent_obj.create_response()
            response.async_set_error(
                intent.IntentResponseErrorCode.FAILED_TO_HANDLE,
                "Could not find the Telegram Bot Conversation configuration entry "
                "matching the event.",
            )
            return response

        if not callable(method := getattr(conversation_handler, self.method, None)):
            response = intent_obj.create_response()
            response.async_set_error(
                intent.IntentResponseErrorCode.FAILED_TO_HANDLE,
                "The Telegram Bot Conversation handler does not support this intent. "
                "This could be due to broken installation.",
            )

        response = intent_obj.create_response()
        try:
            result = await method(event, context, **slots)  # type: ignore[misc]
            response.async_set_speech(result)
        except HomeAssistantError as e:
            response.async_set_error(
                intent.IntentResponseErrorCode.FAILED_TO_HANDLE, str(e)
            )

        return response


class GenerateImageHandler(BaseTelegramBotConversationIntentHandler):
    """Handle generate image intents."""

    intent_type = INTENT_GENERATE_IMAGE
    description = "Generate an image using AI and send it to the user via Telegram."
    method = "handle_generate_image_intent"

    @property
    def slot_schema(self) -> dict[vol.Marker, Any] | None:
        """Return a slot schema."""
        return {
            vol.Required("prompt"): intent.non_empty_string,
        }
