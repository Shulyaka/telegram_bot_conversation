"""Intents for the Telegram Bot Conversation integration."""

from __future__ import annotations

import voluptuous as vol

from custom_components.telegram_bot_conversation.const import DOMAIN
from homeassistant.components.telegram_bot.const import (
    CONF_CONFIG_ENTRY_ID,
    EVENT_TELEGRAM_ATTACHMENT,
    EVENT_TELEGRAM_TEXT,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import intent

from .const import CONF_TELEGRAM_ENTRY

INTENT_GENERATE_IMAGE = "TelegramGenerateImage"


async def async_setup_intents(hass: HomeAssistant) -> None:
    """Set up the telegram intents."""
    intent.async_register(hass, GenerateImageHandler())


class BaseTelegramBotConversationIntentHandler(intent.IntentHandler):
    """Base class for Telegram Bot Conversation intent handlers."""

    platforms = None

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Handle the intent."""
        hass = intent_obj.hass
        slots = {
            k: v["value"]
            for k, v in self.async_validate_slots(intent_obj.slots).items()
        }

        if (
            not (context := intent_obj.context)
            or not (event := context.origin_event)
            or event.event_type not in (EVENT_TELEGRAM_TEXT, EVENT_TELEGRAM_ATTACHMENT)
        ):
            response = intent_obj.create_response()
            response.async_set_error(
                intent.IntentResponseErrorCode.FAILED_TO_HANDLE,
                "This tool is only supported for the Telegram Bot Conversation integration.",
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

        config_entry = None
        for entry in hass.config_entries.async_entries(DOMAIN):
            if (
                entry.state == ConfigEntryState.LOADED
                and entry.data[CONF_TELEGRAM_ENTRY] == telegram_entry_id
            ):
                config_entry = entry
                break

        if not config_entry or not (conversation_handler := config_entry.runtime_data):
            response = intent_obj.create_response()
            response.async_set_error(
                intent.IntentResponseErrorCode.FAILED_TO_HANDLE,
                "Could not find the Telegram Bot Conversation configuration entry matching the event.",
            )
            return response

        method = getattr(conversation_handler, self.method, None)

        response = intent_obj.create_response()
        try:
            result = await method(event, **slots)
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
    slot_schema = {
        vol.Required("prompt"): intent.non_empty_string,
    }
    method = "handle_generate_image_intent"
