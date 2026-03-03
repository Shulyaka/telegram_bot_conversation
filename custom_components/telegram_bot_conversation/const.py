"""Constants for telegram_bot_conversation."""

from logging import Logger, getLogger
from datetime import timedelta

LOGGER: Logger = getLogger(__package__)

DOMAIN = "telegram_bot_conversation"

CONF_CONVERSATION_TIMEOUT = "conversation_timeout"
CONF_CONVERSATION_AGENT = "conversation_agent"
CONF_TELEGRAM_ENTRY = "telegram_entry"
CONF_TELEGRAM_SUBENTRY = "telegram_subentry"
CONF_USER = "user_id"

DEFAULT_CONVERSATION_TIMEOUT = timedelta(hours=24)
