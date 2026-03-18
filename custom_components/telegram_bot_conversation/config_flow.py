"""Config flow for Telegram Bot Conversation custom integration."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import voluptuous as vol

from homeassistant.components.telegram_bot.const import (
    DOMAIN as TELEGRAM_DOMAIN,
    SUBENTRY_TYPE_ALLOWED_CHAT_IDS,
)
from homeassistant.config_entries import ConfigSubentryData, ConfigSubentryFlow
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv, selector

from . import TelegramBotConversationConfigEntry
from .const import (
    CONF_ATTACHMENTS,
    CONF_CONVERSATION_AGENT,
    CONF_CONVERSATION_TIMEOUT,
    CONF_DISABLE_WEB_PREV,
    CONF_LATEX,
    CONF_MERMAID,
    CONF_TELEGRAM_ENTRY,
    CONF_TELEGRAM_SUBENTRY,
    CONF_THOUGHTS,
    CONF_TMPDIR,
    CONF_USER,
    DOMAIN,
)
from .recursive_data_flow import AbortRecursiveFlow, RecursiveConfigFlow


class TelegramBotConversationFlow(RecursiveConfigFlow, domain=DOMAIN):
    """Handle config and options flow for Telegram Bot Conversation."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_validate_input(
        self, step_id: str, user_input: dict[str, Any]
    ) -> dict[str, str]:
        """Validate step data."""
        if step_id == "user":
            self._async_abort_entries_match(user_input)
        elif step_id == "init" and isinstance(self, ConfigSubentryFlow):
            for subentry in self._get_entry().subentries.values():
                if subentry.data.get(CONF_TELEGRAM_SUBENTRY) == user_input[
                    CONF_TELEGRAM_SUBENTRY
                ] and (
                    self.source == "user"
                    or subentry.subentry_id
                    != self._get_reconfigure_subentry().subentry_id
                ):
                    return {
                        CONF_TELEGRAM_SUBENTRY: "telegram_subentry_already_configured"
                    }

        return {}

    async def get_data_schema(self) -> vol.Schema:
        """Get data schema."""
        for telegram_entry in self.hass.config_entries.async_entries(TELEGRAM_DOMAIN):
            return vol.Schema(
                {
                    vol.Required(
                        CONF_TELEGRAM_ENTRY, default=telegram_entry.entry_id
                    ): selector.ConfigEntrySelector(
                        selector.ConfigEntrySelectorConfig(
                            integration=TELEGRAM_DOMAIN,
                        )
                    ),
                }
            )
        raise AbortRecursiveFlow("no_telegram_bot_entries")

    async def get_options_schema(self) -> vol.Schema:
        """Get options schema."""
        return vol.Schema(
            {
                vol.Required(
                    CONF_CONVERSATION_TIMEOUT, default={"minutes": 30}
                ): selector.DurationSelector(),
                vol.Required(
                    CONF_TMPDIR, default=self.hass.config.path("www")
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(self.hass.config.allowlist_external_dirs),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_ATTACHMENTS, default=20): cv.positive_int,
                vol.Optional(CONF_LATEX, default=True): bool,
                vol.Optional(CONF_MERMAID, default=True): bool,
                vol.Optional(CONF_DISABLE_WEB_PREV, default=False): bool,
                vol.Optional(CONF_THOUGHTS, default=True): bool,
            }
        )

    @property
    def title(self) -> str:
        """Return config flow title."""
        if (telegram_entry_id := self.data.get(CONF_TELEGRAM_ENTRY)) and (
            telegram_entry := self.hass.config_entries.async_get_entry(
                telegram_entry_id
            )
        ):
            return telegram_entry.title

        return super().title

    @property
    def subentry_title(self) -> str:
        """Return config subentry flow title."""
        if (
            (telegram_entry_id := self.data.get(CONF_TELEGRAM_ENTRY))
            and (
                telegram_entry := self.hass.config_entries.async_get_entry(
                    telegram_entry_id
                )
            )
            and (telegram_subentry_id := self.options.get(CONF_TELEGRAM_SUBENTRY))
            and (
                telegram_subentry := telegram_entry.subentries.get(telegram_subentry_id)
            )
        ):
            return telegram_subentry.title

        return super().title

    @classmethod
    @callback
    def get_subentries(
        cls, config_entry: TelegramBotConversationConfigEntry
    ) -> Iterable[str]:
        """Get subentries list."""
        return ["telegram_id"]

    async def get_subentry_schema(self, subentry_type: str) -> vol.Schema:
        """Get subentry schema."""
        known_telegram_subentries = {
            subentry.data.get(CONF_TELEGRAM_SUBENTRY)
            for subentry in self._get_entry().subentries.values()
            if subentry.data.get(CONF_TELEGRAM_SUBENTRY)
            and (
                self.source == "user"
                or subentry.subentry_id != self._get_reconfigure_subentry().subentry_id
            )
        }
        if (telegram_entry_id := self.data.get(CONF_TELEGRAM_ENTRY)) and (
            telegram_entry := self.hass.config_entries.async_get_entry(
                telegram_entry_id
            )
        ):
            telegram_subentry_options = [
                selector.SelectOptionDict(
                    value=telegram_subentry_id, label=telegram_subentry.title
                )
                for telegram_subentry_id, telegram_subentry in telegram_entry.subentries.items()
                if telegram_subentry.subentry_type == SUBENTRY_TYPE_ALLOWED_CHAT_IDS
                and telegram_subentry_id not in known_telegram_subentries
            ]
        else:
            telegram_subentry_options = []

        if not telegram_subentry_options:
            raise AbortRecursiveFlow("all_telegram_entries_configured")

        user_options = [
            selector.SelectOptionDict(value=user.id, label=user.name)
            for user in await self.hass.auth.async_get_users()
            if not user.system_generated
        ]

        return vol.Schema(
            {
                vol.Required(CONF_TELEGRAM_SUBENTRY): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=telegram_subentry_options,
                        translation_key=CONF_TELEGRAM_SUBENTRY,
                        multiple=False,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    ),
                ),
                vol.Optional(
                    CONF_USER,
                    default=await self._get_user_id(
                        telegram_subentry_options[0]["label"]
                    )
                    or vol.UNDEFINED,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=user_options,
                        translation_key=CONF_USER,
                        multiple=False,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    ),
                ),
                vol.Optional(
                    CONF_CONVERSATION_AGENT
                ): selector.ConversationAgentSelector(),
            }
        )

    async def get_default_subentries(self) -> Iterable[ConfigSubentryData] | None:
        """Get default subentries."""
        if (telegram_entry_id := self.data.get(CONF_TELEGRAM_ENTRY)) and (
            telegram_entry := self.hass.config_entries.async_get_entry(
                telegram_entry_id
            )
        ):
            return [
                {
                    "subentry_type": "telegram_id",
                    "data": {
                        CONF_TELEGRAM_SUBENTRY: telegram_subentry_id,
                        CONF_USER: await self._get_user_id(telegram_subentry.title),
                    },
                    "title": telegram_subentry.title,
                    "unique_id": None,
                }
                for telegram_subentry_id, telegram_subentry in telegram_entry.subentries.items()
            ] or None
        return None

    async def _get_user_id(self, name: str) -> str | None:
        for user in await self.hass.auth.async_get_users():
            if user.name == name and not user.system_generated:
                return user.id
        return None
