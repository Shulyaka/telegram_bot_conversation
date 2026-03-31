"""Constants for telegram_bot_conversation."""

from enum import StrEnum
from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "telegram_bot_conversation"

CONF_CONVERSATION_TIMEOUT = "conversation_timeout"
CONF_CONVERSATION_AGENT = "conversation_agent"
CONF_TELEGRAM_ENTRY = "telegram_entry"
CONF_TELEGRAM_SUBENTRY = "telegram_subentry"
CONF_USER = "user_id"
CONF_ATTACHMENTS = "attachments"
CONF_LATEX = "latex"
CONF_MERMAID = "mermaid"
CONF_TMPDIR = "tmpdir"
CONF_WEB_PREVIEW = "web_page_preview"
CONF_THOUGHTS = "thoughts"
CONF_AI_TASK = "ai_task"


class WebPreview(StrEnum):
    """Web page preview options."""

    ON = "on"
    OFF = "off"
    LAST = "last"


REACTION_EMOJI = [
    "❤",
    "👍",
    "👎",
    "🔥",
    "🥰",
    "👏",
    "😁",
    "🤔",
    "🤯",
    "😱",
    "🤬",
    "😢",
    "🎉",
    "🤩",
    "🤮",
    "💩",
    "🙏",
    "👌",
    "🕊",
    "🤡",
    "🥱",
    "🥴",
    "😍",
    "🐳",
    "❤‍🔥",
    "🌚",
    "🌭",
    "💯",
    "🤣",
    "⚡",
    "🍌",
    "🏆",
    "💔",
    "🤨",
    "😐",
    "🍓",
    "🍾",
    "💋",
    "🖕",
    "😈",
    "😴",
    "😭",
    "🤓",
    "👻",
    "👨‍💻",
    "👀",
    "🎃",
    "🙈",
    "😇",
    "😨",
    "🤝",
    "✍",
    "🤗",
    "🫡",
    "🎅",
    "🎄",
    "☃",
    "💅",
    "🤪",
    "🗿",
    "🆒",
    "💘",
    "🙉",
    "🦄",
    "😘",
    "💊",
    "🙊",
    "😎",
    "👾",
    "🤷‍♂",
    "🤷",
    "🤷‍♀",
    "😡",
]
