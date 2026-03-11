"""Constants for telegram_bot_conversation tests."""

from custom_components.telegram_bot_conversation.const import (
    CONF_ATTACHMENTS,
    CONF_CONVERSATION_TIMEOUT,
    CONF_LATEX,
    CONF_MERMAID,
    CONF_TMPDIR,
)

MOCK_OPTIONS_CONFIG = {
    CONF_CONVERSATION_TIMEOUT: {"minutes": 15},
    CONF_ATTACHMENTS: True,
    CONF_LATEX: False,
    CONF_MERMAID: False,
    CONF_TMPDIR: "/mnt/share/media",
}
