"""Constants for telegram_bot_conversation tests."""

from custom_components.telegram_bot_conversation.const import (
    CONF_ATTACHMENTS,
    CONF_CONVERSATION_TIMEOUT,
    CONF_DISABLE_WEB_PREV,
    CONF_LATEX,
    CONF_MERMAID,
    CONF_TMPDIR,
)

MOCK_OPTIONS_CONFIG = {
    CONF_CONVERSATION_TIMEOUT: {"minutes": 15},
    CONF_ATTACHMENTS: 15,
    CONF_LATEX: False,
    CONF_MERMAID: False,
    CONF_DISABLE_WEB_PREV: True,
    CONF_TMPDIR: "/mnt/share/media",
}
