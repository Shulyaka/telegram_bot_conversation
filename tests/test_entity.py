"""Tests for telegram_bot_conversation entity."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_mock_service,
)

from custom_components.telegram_bot_conversation.const import (
    CONF_CONVERSATION_AGENT,
    DOMAIN,
)
from custom_components.telegram_bot_conversation.entity import (
    MAX_TELEGRAM_LENGTH,
    ConversationConfig,
)
from homeassistant.components import conversation
from homeassistant.components.telegram_bot.const import (
    ATTR_CALLBACK_QUERY_ID,
    ATTR_CHAT_ACTION,
    ATTR_CHAT_ID,
    ATTR_DISABLE_WEB_PREV,
    ATTR_FILE,
    ATTR_FILE_ID,
    ATTR_FILE_MIME_TYPE,
    ATTR_FILE_PATH,
    ATTR_IS_BIG,
    ATTR_KEYBOARD_INLINE,
    ATTR_MESSAGE,
    ATTR_MESSAGE_ID,
    ATTR_MESSAGE_TAG,
    ATTR_MESSAGE_THREAD_ID,
    ATTR_MSGID,
    ATTR_PARSER,
    ATTR_REACTION,
    ATTR_TEXT,
    CHAT_ACTION_UPLOAD_PHOTO,
    DOMAIN as TELEGRAM_DOMAIN,
    SERVICE_ANSWER_CALLBACK_QUERY,
    SERVICE_DOWNLOAD_FILE,
    SERVICE_EDIT_REPLYMARKUP,
    SERVICE_SEND_CHAT_ACTION,
    SERVICE_SEND_MESSAGE,
    SERVICE_SEND_PHOTO,
    SERVICE_SET_MESSAGE_REACTION,
)
from homeassistant.core import Context, Event, HomeAssistant
from homeassistant.helpers.chat_session import async_get_chat_session


def _first_chat_handler(mock_config_entry):
    """Return the first runtime chat handler."""
    subentry = next(iter(mock_config_entry.subentries.values()))
    return next(iter(mock_config_entry.runtime_data.chat_handlers.values())), subentry


async def test_conversation_stream(
    hass: HomeAssistant,
    mock_receive_telegram_message: Callable[[str], Awaitable[None]],
    mock_conversation_agent: AsyncMock,
    mock_config_entry,
) -> None:
    """Test plain text conversation using a streaming agent."""
    mock_conversation_agent.return_value = [
        conversation.AssistantContentDeltaDict(
            {"role": "assistant", "content": "Hello!"}
        )
    ]
    events = async_capture_events(hass, "telegram_sent")

    await mock_receive_telegram_message("Hi!")

    assert len(events) == 1
    event = events[0]
    assert event.data[ATTR_CHAT_ID] == 12345678
    assert event.data[ATTR_MESSAGE_THREAD_ID] == 0
    assert event.context.user_id is not None

    mock_conversation_agent.assert_awaited_once()
    user_content = mock_conversation_agent.await_args.args[0]
    assert user_content.role == "user"
    assert user_content.content == "Hi!"

    chat_log = next(iter(hass.data.get(conversation.chat_log.DATA_CHAT_LOGS).values()))

    assert len(chat_log.content) == 3
    assert chat_log.content[1].content == "Hi!"
    assert chat_log.content[2].content == "Hello!"
    assert chat_log.content[2].agent_id == "Mock Agent ID"


async def test_conversation_nonstream(
    hass: HomeAssistant,
    mock_receive_telegram_message: Callable[[str], Awaitable[None]],
    mock_config_entry,
) -> None:
    """Test plain text conversation using a non-streaming agent."""
    subentry = next(iter(mock_config_entry.subentries.values()))
    hass.config_entries.async_update_subentry(
        mock_config_entry,
        subentry,
        data={k: v for k, v in subentry.data.items() if k != CONF_CONVERSATION_AGENT},
    )

    events = async_capture_events(hass, "telegram_sent")

    await mock_receive_telegram_message("Hi!")

    assert len(events) == 1
    event = events[0]
    assert event.data[ATTR_CHAT_ID] == 12345678
    assert event.data[ATTR_MESSAGE_THREAD_ID] == 0
    assert event.context.user_id is not None

    chat_log = next(iter(hass.data.get(conversation.chat_log.DATA_CHAT_LOGS).values()))

    assert len(chat_log.content) == 5
    assert chat_log.content[1].content == "Hi!"
    assert chat_log.content[-1].content == "Hello from Home Assistant."
    assert chat_log.content[-1].agent_id == "conversation.home_assistant"


async def test_prompt(
    hass: HomeAssistant,
    mock_receive_telegram_message: Callable[[str], Awaitable[None]],
    mock_conversation_agent: AsyncMock,
    mock_config_entry,
) -> None:
    """Test prompt."""
    mock_conversation_agent.return_value = [
        conversation.AssistantContentDeltaDict(
            {"role": "assistant", "content": "Hello!"}
        )
    ]
    await mock_receive_telegram_message("Hi!")

    chat_log = next(iter(hass.data.get(conversation.chat_log.DATA_CHAT_LOGS).values()))

    prompt = chat_log.content[0].content

    assert (
        "The user is interacting through Telegram. Markdown is fully supported."
        in prompt
    )


async def test_model_command_changes_agent(
    hass: HomeAssistant,
    mock_config_entry,
    mock_conversation_agent: AsyncMock,
) -> None:
    """Test /model can switch the configured conversation agent."""
    chat_handler, _ = _first_chat_handler(mock_config_entry)
    context = Context()

    with (
        patch.object(
            chat_handler, "_async_change_agent", AsyncMock(return_value=True)
        ) as change_agent,
        patch.object(chat_handler, "send_message", AsyncMock()) as send_message,
    ):
        await chat_handler.async_process_command(
            0,
            "/model",
            ["conversation.home_assistant"],
            context,
        )

    change_agent.assert_awaited_once_with("conversation.home_assistant")
    send_message.assert_awaited_once_with(
        message="Conversation agent switched to: `Home Assistant`",
        thread_id=0,
        context=context,
    )


async def test_model_command_shows_agent_keyboard(
    hass: HomeAssistant,
    mock_config_entry,
    mock_conversation_agent: AsyncMock,
) -> None:
    """Test /model without arguments sends an inline agent picker."""
    chat_handler, _ = _first_chat_handler(mock_config_entry)
    context = Context()
    edit_reply_markup_calls = async_mock_service(
        hass,
        TELEGRAM_DOMAIN,
        SERVICE_EDIT_REPLYMARKUP,
    )

    with patch.object(
        chat_handler,
        "send_message",
        AsyncMock(
            return_value={
                "chats": [
                    {
                        ATTR_CHAT_ID: chat_handler.chat_id,
                        ATTR_MESSAGE_ID: 987,
                    }
                ]
            }
        ),
    ) as send_message:
        await chat_handler.async_process_command(0, "/model", [], context)

    send_message.assert_awaited_once_with(
        message="Current conversation agent: `Mock Agent ID`",
        thread_id=0,
        context=context,
    )
    assert len(edit_reply_markup_calls) == 1
    call = edit_reply_markup_calls[0]
    assert call.data[ATTR_MESSAGE_ID] == 987
    assert ("Home Assistant", "/model conversation.home_assistant") in [
        button for row in call.data[ATTR_KEYBOARD_INLINE] for button in row
    ]


async def test_reaction_prefix_is_sent_as_message_reaction(
    hass: HomeAssistant,
    mock_config_entry,
) -> None:
    """Test assistant responses starting with a supported emoji set a reaction."""
    chat_handler, _ = _first_chat_handler(mock_config_entry)
    context = Context()
    thread_id = 0
    reaction_calls = async_mock_service(
        hass,
        TELEGRAM_DOMAIN,
        SERVICE_SET_MESSAGE_REACTION,
    )

    with (
        async_get_chat_session(hass, f"telegram_{chat_handler.chat_id}") as session,
        conversation.async_get_chat_log(
            hass,
            session,
        ) as chat_log,
        patch.object(chat_handler, "send_message", AsyncMock()) as send_message,
    ):
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(
                agent_id="Mock Agent ID",
                content="Reaction accepted",
            )
        )
        await chat_handler.async_chat_log_delta_listener(
            chat_log,
            {"role": "assistant"},
            thread_id,
            321,
            context,
        )
        await chat_handler.async_chat_log_delta_listener(
            chat_log,
            {"content": "👍 Reaction accepted"},
            thread_id,
            321,
            context,
        )
        await chat_handler.async_chat_log_delta_listener(
            chat_log,
            {"role": None},
            thread_id,
            321,
            context,
        )

    assert len(reaction_calls) == 1
    assert reaction_calls[0].data[ATTR_CHAT_ID] == chat_handler.chat_id
    assert reaction_calls[0].data[ATTR_MESSAGE_ID] == 321
    assert reaction_calls[0].data[ATTR_REACTION] == "👍"
    assert reaction_calls[0].data[ATTR_IS_BIG] is True
    send_message.assert_any_await(
        message="Reaction accepted",
        thread_id=thread_id,
        context=context,
    )


async def test_send_message_splits_long_text(
    hass: HomeAssistant,
    mock_config_entry,
) -> None:
    """Test long responses are split at Telegram's message length limit."""
    chat_handler, _ = _first_chat_handler(mock_config_entry)
    context = Context()
    calls = async_mock_service(
        hass,
        TELEGRAM_DOMAIN,
        SERVICE_SEND_MESSAGE,
        response={"chats": []},
    )

    await chat_handler.send_message(
        message="x" * (MAX_TELEGRAM_LENGTH + 20),
        thread_id=0,
        context=context,
    )

    assert len(calls) == 2
    assert all(len(call.data[ATTR_MESSAGE]) <= MAX_TELEGRAM_LENGTH for call in calls)
    assert all(call.data[ATTR_PARSER] == "markdownv2" for call in calls)
    assert calls[0].data[ATTR_DISABLE_WEB_PREV] is True
    assert calls[1].data[ATTR_DISABLE_WEB_PREV] is False
    assert calls[0].data[ATTR_MESSAGE_TAG] == DOMAIN


async def test_text_attachment_is_inserted_inline(
    hass: HomeAssistant,
    mock_config_entry,
    mock_conversation_agent: AsyncMock,
    tmp_path: Path,
) -> None:
    """Test text file attachments are passed to the agent as inline fenced text."""
    chat_handler, _ = _first_chat_handler(mock_config_entry)
    attachment_path = tmp_path / "notes.py"
    attachment_path.write_text("print('hello')\n")
    mock_conversation_agent.return_value = [
        conversation.AssistantContentDeltaDict(
            {"role": "assistant", "content": "Read it."}
        )
    ]
    async_mock_service(
        hass,
        TELEGRAM_DOMAIN,
        SERVICE_DOWNLOAD_FILE,
        response={ATTR_FILE_PATH: attachment_path.as_posix()},
    )

    await chat_handler.async_process_message(
        Event(
            "telegram_attachment",
            {
                ATTR_CHAT_ID: chat_handler.chat_id,
                ATTR_FILE_ID: "telegram-file-id",
                ATTR_FILE_MIME_TYPE: "text/x-python",
                ATTR_TEXT: "Please review this",
                ATTR_MESSAGE_THREAD_ID: 0,
            },
            context=Context(),
        )
    )

    mock_conversation_agent.assert_awaited_once()
    user_content = mock_conversation_agent.await_args.args[0]
    assert user_content.content == (
        "notes.py:\n```x-python\nprint('hello')\n\n```\n\nPlease review this"
    )


async def test_threaded_message_uses_thread_specific_conversation(
    hass: HomeAssistant,
    mock_config_entry,
    mock_conversation_agent: AsyncMock,
) -> None:
    """Test Telegram topic messages use separate conversation history."""
    chat_handler, _ = _first_chat_handler(mock_config_entry)
    thread_id = 77
    mock_conversation_agent.return_value = [
        conversation.AssistantContentDeltaDict(
            {"role": "assistant", "content": "Thread reply"}
        )
    ]
    context = Context()

    with patch.object(chat_handler, "send_message", AsyncMock()) as send_message:
        await chat_handler.async_process_message(
            Event(
                "telegram_text",
                {
                    ATTR_CHAT_ID: chat_handler.chat_id,
                    ATTR_TEXT: "Topic question",
                    ATTR_MESSAGE_THREAD_ID: thread_id,
                },
                context=context,
            )
        )

    mock_conversation_agent.assert_awaited_once()
    user_content = mock_conversation_agent.await_args.args[0]
    assert user_content.content == "Topic question"
    assert (
        f"telegram_{chat_handler.chat_id}_{thread_id}"
        in hass.data[conversation.chat_log.DATA_CHAT_LOGS]
    )
    assert (
        f"telegram_{chat_handler.chat_id}"
        not in hass.data[conversation.chat_log.DATA_CHAT_LOGS]
    )
    send_message.assert_any_await(
        message="Thread reply",
        thread_id=thread_id,
        context=context,
    )


async def test_callback_processes_command_and_acknowledges_query(
    hass: HomeAssistant,
    mock_config_entry,
) -> None:
    """Test inline keyboard callbacks dispatch commands and clear the keyboard."""
    chat_handler, _ = _first_chat_handler(mock_config_entry)
    thread_id = 42
    answer_calls = async_mock_service(
        hass,
        TELEGRAM_DOMAIN,
        SERVICE_ANSWER_CALLBACK_QUERY,
    )
    edit_reply_markup_calls = async_mock_service(
        hass,
        TELEGRAM_DOMAIN,
        SERVICE_EDIT_REPLYMARKUP,
    )

    with patch.object(
        chat_handler,
        "async_process_command",
        AsyncMock(),
    ) as process_command:
        await chat_handler.async_handle_callback(
            Event(
                "telegram_callback",
                {
                    ATTR_CHAT_ID: chat_handler.chat_id,
                    "data": "/model conversation.home_assistant",
                    ATTR_MESSAGE: {
                        ATTR_MESSAGE_ID: 654,
                        ATTR_MESSAGE_THREAD_ID: thread_id,
                    },
                    ATTR_MSGID: "callback-query-id",
                },
                context=Context(),
            )
        )

    process_command.assert_awaited_once()
    assert process_command.await_args.args[:3] == (
        thread_id,
        "/model",
        ["conversation.home_assistant"],
    )
    assert len(answer_calls) == 1
    assert answer_calls[0].data[ATTR_CALLBACK_QUERY_ID] == "callback-query-id"
    assert answer_calls[0].data[ATTR_MESSAGE] == "Done"
    assert len(edit_reply_markup_calls) == 1
    assert edit_reply_markup_calls[0].data[ATTR_MESSAGE_ID] == 654
    assert edit_reply_markup_calls[0].data[ATTR_KEYBOARD_INLINE] == []


async def test_binary_attachment_is_passed_to_agent(
    hass: HomeAssistant,
    mock_config_entry,
    mock_conversation_agent: AsyncMock,
    tmp_path: Path,
) -> None:
    """Test non-text attachments are attached to the conversation request."""
    chat_handler, _ = _first_chat_handler(mock_config_entry)
    attachment_path = tmp_path / "image.png"
    attachment_path.write_bytes(b"not really a png")
    mock_conversation_agent.return_value = [
        conversation.AssistantContentDeltaDict(
            {"role": "assistant", "content": "Image noted."}
        )
    ]
    async_mock_service(
        hass,
        TELEGRAM_DOMAIN,
        SERVICE_DOWNLOAD_FILE,
        response={ATTR_FILE_PATH: attachment_path.as_posix()},
    )

    with patch.object(chat_handler, "send_message", AsyncMock()):
        await chat_handler.async_process_message(
            Event(
                "telegram_attachment",
                {
                    ATTR_CHAT_ID: chat_handler.chat_id,
                    ATTR_FILE_ID: "telegram-image-id",
                    ATTR_FILE_MIME_TYPE: "image/png",
                    ATTR_TEXT: "What is this?",
                    ATTR_MESSAGE_THREAD_ID: 0,
                },
                context=Context(),
            )
        )

    mock_conversation_agent.assert_awaited_once()
    user_content = mock_conversation_agent.await_args.args[0]
    assert user_content.content == "What is this?"
    assert len(user_content.attachments) == 1
    attachment = user_content.attachments[0]
    assert attachment.mime_type == "image/png"
    assert (
        attachment.media_content_id == "media-source://telegram_bot/telegram-image-id"
    )
    assert attachment.path == attachment_path


async def test_attachment_download_error_is_sent_to_chat(
    hass: HomeAssistant,
    mock_config_entry,
) -> None:
    """Test attachment download failures are reported back to Telegram."""
    chat_handler, _ = _first_chat_handler(mock_config_entry)
    async_mock_service(
        hass,
        TELEGRAM_DOMAIN,
        SERVICE_DOWNLOAD_FILE,
        response={},
    )

    with (
        patch.object(chat_handler, "send_message", AsyncMock()) as send_message,
        pytest.raises(KeyError),
    ):
        await chat_handler.async_process_message(
            Event(
                "telegram_attachment",
                {
                    ATTR_CHAT_ID: chat_handler.chat_id,
                    ATTR_FILE_ID: "telegram-file-id",
                    ATTR_FILE_MIME_TYPE: "image/png",
                    ATTR_MESSAGE_THREAD_ID: 0,
                },
                context=Context(),
            )
        )

    send_message.assert_awaited_once()
    assert send_message.await_args.kwargs["message"].startswith("Error:")
    assert send_message.await_args.kwargs["thread_id"] == 0


async def test_generate_image_handler_sends_photo(
    hass: HomeAssistant,
    mock_config_entry,
    tmp_path: Path,
) -> None:
    """Test image generation sends upload action and generated media."""
    chat_handler, _ = _first_chat_handler(mock_config_entry)
    context = Context()
    image_path = tmp_path / "generated.png"
    image_path.write_bytes(b"png")
    chat_action_calls = async_mock_service(
        hass,
        TELEGRAM_DOMAIN,
        SERVICE_SEND_CHAT_ACTION,
    )
    photo_calls = async_mock_service(
        hass,
        TELEGRAM_DOMAIN,
        SERVICE_SEND_PHOTO,
        response={"chats": []},
    )

    with (
        patch(
            "custom_components.telegram_bot_conversation.entity.async_generate_image",
            AsyncMock(
                return_value={
                    "media_source_id": "media-source://generated/image",
                    "revised_prompt": "A clearer lighthouse prompt",
                }
            ),
        ) as generate_image,
        patch(
            "custom_components.telegram_bot_conversation.entity.async_resolve_media",
            AsyncMock(return_value=type("Media", (), {"path": image_path})()),
        ) as resolve_media,
    ):
        message = await chat_handler.handle_generate_image_intent(
            Event(
                "telegram_text",
                {
                    ATTR_CHAT_ID: chat_handler.chat_id,
                    ATTR_MESSAGE_THREAD_ID: 12,
                },
                context=context,
            ),
            context,
            "Draw a lighthouse",
        )

    generate_image.assert_awaited_once_with(
        hass,
        task_name=DOMAIN,
        entity_id=None,
        instructions="Draw a lighthouse",
    )
    resolve_media.assert_awaited_once_with(
        hass,
        "media-source://generated/image",
        None,
    )
    assert len(chat_action_calls) == 1
    assert chat_action_calls[0].data[ATTR_CHAT_ACTION] == CHAT_ACTION_UPLOAD_PHOTO
    assert chat_action_calls[0].data[ATTR_MESSAGE_THREAD_ID] == 12
    assert len(photo_calls) == 1
    assert photo_calls[0].data[ATTR_FILE] == image_path.as_posix()
    assert photo_calls[0].data[ATTR_MESSAGE_THREAD_ID] == 12
    assert (
        message
        == "The image has been generated and sent to the user. Revised prompt: A clearer lighthouse prompt"
    )


async def test_new_text_cancels_active_conversation(
    hass: HomeAssistant,
    mock_config_entry,
) -> None:
    """Test a new Telegram message interrupts the active conversation task."""
    chat_handler, _ = _first_chat_handler(mock_config_entry)
    started = asyncio.Event()

    async def never_finish() -> None:
        started.set()
        await asyncio.Event().wait()

    old_task = hass.async_create_task(never_finish())
    await started.wait()
    current_conversation = chat_handler.conversations.setdefault(
        0, ConversationConfig()
    )
    current_conversation.task = old_task
    event = Event(
        "telegram_text",
        {
            ATTR_CHAT_ID: chat_handler.chat_id,
            ATTR_TEXT: "Interrupting question",
            ATTR_MESSAGE_THREAD_ID: 0,
        },
        context=Context(),
    )

    with patch.object(
        chat_handler,
        "async_process_message",
        AsyncMock(),
    ) as process_message:
        await chat_handler.async_handle_text(event)
        await hass.async_block_till_done()

    assert old_task.cancelled()
    process_message.assert_awaited_once_with(event)
    assert current_conversation.task is None
