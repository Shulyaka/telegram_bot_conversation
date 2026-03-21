"""Tests for telegram_bot_conversation runtime setup."""

from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.telegram_bot_conversation import TelegramBotConversationHandler
from custom_components.telegram_bot_conversation.const import (
    CONF_ATTACHMENTS,
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
from custom_components.telegram_bot_conversation.recursive_data_flow import (
    validate_data,
    validate_options,
    validate_subentry_data,
)
from homeassistant.components.conversation import (
    AssistantContent,
    UserContent,
    async_get_chat_log,
)
from homeassistant.components.conversation.chat_log import DATA_CHAT_LOGS
from homeassistant.components.telegram_bot.const import (
    ATTR_CHAT_ID,
    ATTR_MESSAGE,
    ATTR_MESSAGE_THREAD_ID,
    CONF_CHAT_ID,
    CONF_CONFIG_ENTRY_ID,
    DOMAIN as TELEGRAM_DOMAIN,
    SERVICE_SEND_MESSAGE,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.chat_session import DATA_CHAT_SESSION, async_get_chat_session


async def _get_config(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> tuple[dict, dict, dict]:
    """Extract the main config, subentry data, and options from a config entry."""
    data = await validate_data(hass, config_entry)
    options = await validate_options(hass, config_entry)
    subentry_data = {
        subentry_id: await validate_subentry_data(hass, config_entry, subentry_id)
        for subentry_id, subentry in config_entry.subentries.items()
        if subentry.subentry_type == "telegram_id"
    }
    return data, options, subentry_data


async def test_handler_resolves_notify_entity_id(
    hass: HomeAssistant,
    mock_telegram_config_entry,
    mock_config_entry,
) -> None:
    """Test that chat handler stores the telegram notify entity ID."""
    handler = TelegramBotConversationHandler(
        hass, mock_config_entry, *await _get_config(hass, mock_config_entry)
    )
    entity_registry = er.async_get(hass)

    telegram_subentry_id, telegram_subentry = next(
        iter(mock_telegram_config_entry.subentries.items())
    )
    notify_entity_id = next(
        entity_entry.entity_id
        for entity_entry in er.async_entries_for_config_entry(
            entity_registry, mock_telegram_config_entry.entry_id
        )
        if entity_entry.config_subentry_id == telegram_subentry_id
        and entity_entry.entity_id.startswith("notify.")
    )

    chat_handler = handler.chat_handlers[telegram_subentry.data[CONF_CHAT_ID]]

    assert chat_handler.notify_entity_id == notify_entity_id
    assert chat_handler.subentry_id == next(
        subentry.subentry_id
        for subentry in mock_config_entry.subentries.values()
        if subentry.data[CONF_TELEGRAM_SUBENTRY] == telegram_subentry_id
    )


async def test_send_message_uses_notify_entity_id(
    hass: HomeAssistant,
    mock_telegram_config_entry,
    mock_config_entry,
) -> None:
    """Test that send_message targets the telegram notify entity when available."""
    handler = TelegramBotConversationHandler(
        hass, mock_config_entry, *await _get_config(hass, mock_config_entry)
    )

    # Pick the first chat to test with
    _, telegram_subentry = next(iter(mock_telegram_config_entry.subentries.items()))
    chat_id = telegram_subentry.data[CONF_CHAT_ID]
    chat_handler = handler.chat_handlers[chat_id]

    calls = async_mock_service(
        hass,
        TELEGRAM_DOMAIN,
        SERVICE_SEND_MESSAGE,
        response={"chats": []},
    )

    await chat_handler.send_message(message="Hello", thread_id=0, context=Context())

    assert len(calls) == 1
    call = calls[0]
    assert call.domain == TELEGRAM_DOMAIN
    assert call.service == SERVICE_SEND_MESSAGE
    assert call.data[ATTR_MESSAGE] == "Hello"
    assert call.data[ATTR_ENTITY_ID] == [chat_handler.notify_entity_id]
    assert ATTR_CHAT_ID not in call.data
    assert call.data[ATTR_MESSAGE_THREAD_ID] == 0
    assert call.data[CONF_CONFIG_ENTRY_ID] == mock_telegram_config_entry.entry_id


async def test_new_command_clears_history_immediately(
    hass: HomeAssistant,
    mock_telegram_config_entry,
    mock_config_entry,
) -> None:
    """Test that the /new command drops the stored session and chat log right away."""
    handler = TelegramBotConversationHandler(
        hass, mock_config_entry, *await _get_config(hass, mock_config_entry)
    )

    _, telegram_subentry = next(iter(mock_telegram_config_entry.subentries.items()))
    chat_id = telegram_subentry.data[CONF_CHAT_ID]
    chat_handler = handler.chat_handlers[chat_id]
    conversation_id = f"telegram_{chat_id}"

    with (
        async_get_chat_session(hass, conversation_id) as session,
        async_get_chat_log(hass, session) as chat_log,
    ):
        chat_log.async_add_user_content(UserContent(content="Hello"))
        chat_log.async_add_assistant_content_without_tools(
            AssistantContent(agent_id="test-agent", content="Hi there")
        )

    assert conversation_id in hass.data[DATA_CHAT_SESSION]
    assert conversation_id in hass.data[DATA_CHAT_LOGS]

    context = Context()
    with patch.object(chat_handler, "send_message", AsyncMock()) as send_message:
        await chat_handler.async_process_command(0, "/new", [], context)

    assert conversation_id not in hass.data[DATA_CHAT_SESSION]
    assert conversation_id not in hass.data[DATA_CHAT_LOGS]
    send_message.assert_awaited_once_with(
        message="New conversation started.",
        thread_id=0,
        context=context,
    )

    with (
        async_get_chat_session(hass, conversation_id) as session,
        async_get_chat_log(hass, session) as chat_log,
    ):
        assert [content.role for content in chat_log.content] == ["system"]


async def test_migration_from_v1_1(
    hass: HomeAssistant,
    hass_config_dir: str,
    mock_telegram_config_entry: MockConfigEntry,
) -> None:
    """Test migration from version 1.1."""
    for telegram_subentry in mock_telegram_config_entry.subentries.values():
        await hass.auth.async_create_user(name=telegram_subentry.title)

    async def get_user_id(name: str) -> str | None:
        for user in await hass.auth.async_get_users():
            if user.name == name and not user.system_generated:
                return user.id
        return None

    mock_config_entry = MockConfigEntry(
        title=mock_telegram_config_entry.title,
        domain=DOMAIN,
        version=1,
        minor_version=1,
        data={CONF_TELEGRAM_ENTRY: mock_telegram_config_entry.entry_id},
        options={
            CONF_TMPDIR: hass_config_dir + "/www",
            CONF_CONVERSATION_TIMEOUT: {"minutes": 15},
            CONF_ATTACHMENTS: 15,
            CONF_LATEX: False,
            CONF_MERMAID: False,
            CONF_DISABLE_WEB_PREV: True,
            CONF_THOUGHTS: False,
        },
        subentries_data=[
            {
                "subentry_type": "telegram_id",
                "data": {
                    CONF_TELEGRAM_SUBENTRY: telegram_subentry_id,
                    CONF_USER: await get_user_id(telegram_subentry.title),
                },
                "title": telegram_subentry.title,
                "unique_id": None,
            }
            for telegram_subentry_id, telegram_subentry in mock_telegram_config_entry.subentries.items()
        ],
    )
    mock_config_entry.add_to_hass(hass)

    # Run migration
    with patch(
        "custom_components.telegram_bot_conversation.async_setup_entry",
        return_value=True,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.version == 1
    assert entry.minor_version == 2
    assert entry.data == {CONF_TELEGRAM_ENTRY: mock_telegram_config_entry.entry_id}
    assert entry.options == {CONF_TMPDIR: hass_config_dir + "/www"}
    assert len(entry.subentries) == len(mock_telegram_config_entry.subentries)
    for subentry in entry.subentries.values():
        assert subentry.subentry_type == "telegram_id"
        assert (
            subentry.data[CONF_TELEGRAM_SUBENTRY]
            in mock_telegram_config_entry.subentries
        )
        assert subentry.data[CONF_CONVERSATION_TIMEOUT] == {"minutes": 15}
        assert subentry.data[CONF_ATTACHMENTS] == 15
        assert subentry.data[CONF_LATEX] is False
        assert subentry.data[CONF_MERMAID] is False
        assert subentry.data[CONF_DISABLE_WEB_PREV] is True
        assert subentry.data[CONF_THOUGHTS] is False
