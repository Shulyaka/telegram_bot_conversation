"""Per-chat handler for telegram_bot_conversation."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import contextlib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
import tempfile
from typing import Any, Self

from telegramify_markdown import telegramify
from telegramify_markdown.interpreters import (
    BaseInterpreter,
    FileInterpreter,
    MermaidInterpreter,
    TextInterpreter,
)
from telegramify_markdown.type import ContentTypes

from homeassistant.components.conversation import (
    AssistantContentDeltaDict,
    Attachment,
    ChatLog,
    ConversationEntity,
    ToolResultContent,
    UserContent,
    async_converse,
    async_get_chat_log,
)
from homeassistant.components.conversation.agent_manager import get_agent_manager
from homeassistant.components.conversation.const import DATA_COMPONENT, ChatLogEventType
from homeassistant.components.telegram_bot.const import (
    ATTR_CALLBACK_QUERY_ID,
    ATTR_CAPTION,
    ATTR_CHAT_ACTION,
    ATTR_CHAT_ID,
    ATTR_FILE,
    ATTR_FILE_ID,
    ATTR_FILE_MIME_TYPE,
    ATTR_FILE_PATH,
    ATTR_KEYBOARD_INLINE,
    ATTR_MESSAGE,
    ATTR_MESSAGE_ID,
    ATTR_MESSAGE_THREAD_ID,
    ATTR_MSG,
    ATTR_MSGID,
    ATTR_PARSER,
    ATTR_REACTION,
    ATTR_TEXT,
    CHAT_ACTION_TYPING,
    CONF_CONFIG_ENTRY_ID,
    DOMAIN as TELEGRAM_DOMAIN,
    EVENT_TELEGRAM_SENT,
    SERVICE_ANSWER_CALLBACK_QUERY,
    SERVICE_DOWNLOAD_FILE,
    SERVICE_EDIT_REPLYMARKUP,
    SERVICE_SEND_CHAT_ACTION,
    SERVICE_SEND_DOCUMENT,
    SERVICE_SEND_MESSAGE,
    SERVICE_SEND_PHOTO,
    SERVICE_SET_MESSAGE_REACTION,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.chat_session import (
    CONVERSATION_TIMEOUT,
    async_get_chat_session,
)
from homeassistant.helpers.intent import IntentResponseType
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ATTACHMENTS,
    CONF_CONVERSATION_AGENT,
    CONF_CONVERSATION_TIMEOUT,
    CONF_LATEX,
    CONF_MERMAID,
    CONF_TMPDIR,
    DEFAULT_CONVERSATION_TIMEOUT,
    LOGGER,
    REACTION_EMOJI,
)

MAX_TELEGRAM_LENGTH = 4096


def get_telegram_service_target(
    chat_id: int, notify_entity_id: str | None
) -> dict[str, str | int | list[str]]:
    """Build a telegram service target payload."""
    if notify_entity_id:
        return {ATTR_ENTITY_ID: [notify_entity_id]}
    return {ATTR_CHAT_ID: chat_id}


class TelegramMessageWatcher:
    """Context management class to track sent messages."""

    def __init__(self, hass: HomeAssistant, telegram_config_entry_id: str) -> None:
        """Initialize the watcher."""
        self.hass = hass
        self.telegram_config_entry_id = telegram_config_entry_id
        self._unregister_listener = self.hass.bus.async_listen(
            EVENT_TELEGRAM_SENT,
            self.async_handle_sent,
            self.callback_sent_filter,
        )
        self.sent_messages: list[tuple[int, int]] = []
        self.watchers: dict[tuple[int, int], asyncio.Future[None]] = {}

    @callback
    def callback_sent_filter(self, event_data: Mapping[str, Any]) -> bool:
        """Filter sent events."""
        return (
            event_data.get("bot", {}).get(CONF_CONFIG_ENTRY_ID)
            == self.telegram_config_entry_id
        )

    async def async_handle_sent(self, event: Event) -> None:
        """Handle sent events."""
        message = (event.data[ATTR_CHAT_ID], event.data.get(ATTR_MESSAGE_ID))
        self.sent_messages.append(message)
        if message in self.watchers:
            self.watchers[message].set_result(None)
            del self.watchers[message]

    def wait_message(self, chat_id: int, message_id: int) -> asyncio.Future[None]:
        """Watch for a specific message to be sent."""
        message = (chat_id, message_id)
        if message in self.sent_messages:
            future = asyncio.Future()
            future.set_result(None)
            return future
        self.watchers[message] = asyncio.Future()
        return self.watchers[message]

    def async_cleanup(self) -> None:
        """Clean up the watcher."""
        if not self._unregister_listener:
            return
        self._unregister_listener()
        self._unregister_listener = None

    def __enter__(self) -> Self:
        """Enter the context."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit the context."""
        self.async_cleanup()

    def __del__(self) -> None:
        """Ensure cleanup on deletion."""
        self.async_cleanup()


@dataclass
class ConversationConfig:
    """Per-conversation runtime state tracked across messages."""

    conversation_id: str | None
    task: asyncio.Task | None
    delta_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)
    content_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)
    draft: AssistantContentDeltaDict | None = None


class TelegramChatHandler:
    """Handle conversation logic for a single Telegram chat."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        chat_id: int,
        telegram_entry_id: str,
        user_id: str | None,
        agent_id: str | None,
        notify_entity_id: str | None,
        subentry_id: str,
    ) -> None:
        """Initialize the per-chat handler."""
        self.hass = hass
        self.entry = entry
        self.chat_id = chat_id
        self.telegram_entry_id = telegram_entry_id
        self.user_id = user_id
        self.agent_id = agent_id
        self.notify_entity_id = notify_entity_id
        self.subentry_id = subentry_id
        self.conversations: dict[int, ConversationConfig] = {}

        options = entry.options
        self.interpreter_chain: list[BaseInterpreter] = [TextInterpreter()]
        self.extra_prompt = (
            "The user is interacting through Telegram. Markdown is supported. "
        )
        if options.get(CONF_ATTACHMENTS, True):
            self.interpreter_chain.append(FileInterpreter())
            self.extra_prompt += "Code blocks will be sent as files. "
        if options.get(CONF_MERMAID, True):
            self.interpreter_chain.append(MermaidInterpreter())
            self.extra_prompt += "Mermaid is supported as inline code blocks. "
        self.extra_prompt += (
            f"If the response message starts with any of {REACTION_EMOJI}, "
            "it will be added as a reaction to the user message."
        )

    async def send_message(
        self,
        message: str,
        message_thread_id: int = 0,
        context: Context | None = None,
    ) -> dict[str, list[dict[str, int]]]:
        """Send telegram message, taking care of formatting and length."""
        if context is None:
            context = Context()

        messages: dict[str, list[dict[str, int]]] = {"chats": []}
        created_files: list[Path] = []

        def save_file(file_name: str, file_data: bytes) -> Path:
            """Save temp file."""
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=Path(file_name).stem,
                suffix=Path(file_name).suffix,
                dir=self.entry.options.get(CONF_TMPDIR),
                delete=False,
            ) as temp_file:
                temp_file.write(file_data)
                filename = Path(temp_file.name)
                created_files.append(filename)
                return filename

        try:
            with TelegramMessageWatcher(self.hass, self.telegram_entry_id) as watcher:
                for item in await telegramify(
                    content=message,
                    interpreters_use=self.interpreter_chain,
                    normalize_whitespace=True,
                    latex_escape=self.entry.options.get(CONF_LATEX, True),
                    max_word_count=MAX_TELEGRAM_LENGTH,
                ):
                    if item.content_type == ContentTypes.TEXT:
                        item_messages = await self.hass.services.async_call(
                            TELEGRAM_DOMAIN,
                            SERVICE_SEND_MESSAGE,
                            {
                                ATTR_MESSAGE: item.content,
                                **get_telegram_service_target(
                                    self.chat_id, self.notify_entity_id
                                ),
                                ATTR_MESSAGE_THREAD_ID: message_thread_id,
                                CONF_CONFIG_ENTRY_ID: self.telegram_entry_id,
                                ATTR_PARSER: "markdownv2",
                            },
                            blocking=True,
                            context=context,
                            return_response=True,
                        )
                    elif item.content_type in (ContentTypes.PHOTO, ContentTypes.FILE):
                        item_messages = await self.hass.services.async_call(
                            TELEGRAM_DOMAIN,
                            SERVICE_SEND_PHOTO
                            if item.content_type == ContentTypes.PHOTO
                            else SERVICE_SEND_DOCUMENT,
                            {
                                ATTR_FILE: (
                                    await self.hass.async_add_executor_job(
                                        save_file, item.file_name, item.file_data
                                    )
                                ).as_posix(),
                                ATTR_CAPTION: item.caption,
                                **get_telegram_service_target(
                                    self.chat_id, self.notify_entity_id
                                ),
                                ATTR_MESSAGE_THREAD_ID: message_thread_id,
                                CONF_CONFIG_ENTRY_ID: self.telegram_entry_id,
                                ATTR_PARSER: "markdownv2",
                            },
                            blocking=True,
                            context=context,
                            return_response=True,
                        )

                    messages["chats"].extend(item_messages["chats"])
                    await asyncio.wait_for(
                        asyncio.gather(
                            *(
                                watcher.wait_message(
                                    msg[ATTR_CHAT_ID], msg[ATTR_MESSAGE_ID]
                                )
                                for msg in item_messages["chats"]
                            )
                        ),
                        timeout=10.0,
                    )
        finally:
            if created_files:
                await self.hass.async_add_executor_job(
                    lambda: [file.unlink(missing_ok=True) for file in created_files]
                )

        return messages

    async def async_handle_text(self, event: Event) -> None:
        """Handle text and attachment events."""
        thread_id = event.data.get(ATTR_MESSAGE_THREAD_ID) or 0
        current_conversation = self.conversations.setdefault(
            thread_id, ConversationConfig(None, None)
        )

        if (task := current_conversation.task) and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        task_name = f"telegram_conversation_{self.chat_id}_{thread_id}"
        task = self.hass.async_create_task(
            self.async_process_message(event, current_conversation),
            task_name,
        )
        current_conversation.task = task

        def _clear_task(_task: asyncio.Task) -> None:
            """Clear reference to completed task to avoid retaining tracebacks."""
            if current_conversation.task is _task:
                current_conversation.task = None
            try:
                if err := _task.exception():
                    LOGGER.error(
                        "Error in conversation task for chat_id=%s, thread_id=%s: %s",
                        self.chat_id,
                        thread_id,
                        err,
                        exc_info=err,
                    )
            except asyncio.CancelledError:
                LOGGER.debug(
                    "Conversation task for chat_id=%s, thread_id=%s was cancelled.",
                    self.chat_id,
                    thread_id,
                )

        task.add_done_callback(_clear_task)

    async def async_process_message(
        self, event: Event, current_conversation: ConversationConfig
    ) -> None:
        """Handle conversation task."""
        context = event.context
        if context.user_id is None:
            context.user_id = self.user_id

        error: asyncio.CancelledError | None = None

        @callback
        def chat_log_delta_listener(chat_log: ChatLog, delta: dict[str, Any]) -> None:
            """Handle chat log delta."""

            def log_exceptions(task: asyncio.Task) -> None:
                """Log exceptions from the delta listener."""
                with contextlib.suppress(asyncio.CancelledError):
                    if err := task.exception():
                        LOGGER.error(
                            "Error in chat log delta listener for chat_id=%s, thread_id=%s: %s",
                            self.chat_id,
                            event.data.get(ATTR_MESSAGE_THREAD_ID) or 0,
                            err,
                            exc_info=err,
                        )

            self.hass.async_create_task(
                self.async_chat_log_delta_listener(
                    chat_log,
                    delta,
                    event.data.get(ATTR_MESSAGE_THREAD_ID) or 0,
                    event.data.get(ATTR_MSGID),
                    current_conversation,
                    context,
                ),
                "async_chat_log_delta_listener",
            ).add_done_callback(log_exceptions)

        with (
            async_get_chat_session(
                self.hass, current_conversation.conversation_id
            ) as session,
            async_get_chat_log(
                self.hass,
                session,
                chat_log_delta_listener=chat_log_delta_listener,
            ) as chat_log,
        ):
            current_conversation.conversation_id = session.conversation_id
            if event.data.get(ATTR_FILE_ID):
                file_path = Path(
                    (
                        await self.hass.services.async_call(
                            TELEGRAM_DOMAIN,
                            SERVICE_DOWNLOAD_FILE,
                            {
                                CONF_CONFIG_ENTRY_ID: self.telegram_entry_id,
                                ATTR_FILE_ID: event.data[ATTR_FILE_ID],
                            },
                            blocking=True,
                            context=context,
                            return_response=True,
                        )
                    )[ATTR_FILE_PATH]
                )

                input_text = event.data.get(ATTR_TEXT) or file_path.name
                chat_log.async_add_user_content(
                    UserContent(
                        input_text,  # Must be exactly same text as in async_converse
                        attachments=[
                            Attachment(
                                media_content_id=f"media-source://{TELEGRAM_DOMAIN}/{event.data.get(ATTR_FILE_ID)}",
                                mime_type=event.data.get(ATTR_FILE_MIME_TYPE),
                                path=file_path,
                            )
                        ],
                    )
                )

                def cleanup_file() -> None:
                    """Cleanup temporary file."""
                    file_path.unlink(missing_ok=True)

                @callback
                def cleanup_file_callback() -> None:
                    """Cleanup temporary file."""
                    self.hass.async_add_executor_job(cleanup_file)

                session.async_on_cleanup(cleanup_file_callback)
            else:
                input_text = event.data.get(ATTR_TEXT) or ""

            try:
                conversation_result = await async_converse(
                    self.hass,
                    text=input_text,
                    conversation_id=session.conversation_id,
                    context=context,
                    agent_id=self.agent_id,
                    extra_system_prompt=self.extra_prompt,
                )
            except asyncio.CancelledError as e:
                error = e

            # Flush any remaining delta
            chat_log_delta_listener(chat_log, {"role": None})

        timeout = self.entry.options.get(
            CONF_CONVERSATION_TIMEOUT,
            {"seconds": DEFAULT_CONVERSATION_TIMEOUT.total_seconds()},
        )
        session.last_updated = (
            dt_util.utcnow() + timedelta(**timeout) - CONVERSATION_TIMEOUT
        )

        if error:
            raise error

        if conversation_result.response.response_type == IntentResponseType.ERROR:
            await self.send_message(
                message=conversation_result.response.speech["plain"]["speech"],
                message_thread_id=event.data.get(ATTR_MESSAGE_THREAD_ID) or 0,
                context=context,
            )

    async def async_chat_log_delta_listener(
        self,
        chat_log: ChatLog,
        delta: dict[str, Any],
        thread_id: int,
        msg_id: int | None,
        current_conversation: ConversationConfig,
        context: Context,
    ) -> None:
        """Handle chat log delta."""

        def get_reaction(content: str) -> str | None:
            """Extract reaction from content if it starts with a known reaction emoji."""
            matches = [e for e in REACTION_EMOJI if content.lstrip().startswith(e)]
            return max(matches, key=len) if matches else None

        async with current_conversation.delta_lock:
            if current_conversation.task is None or current_conversation.task.done():
                return

            LOGGER.debug("Chat log delta: %s", delta)
            if "role" in delta:
                if current_conversation.draft:
                    if (
                        (
                            current_conversation.draft["content"]
                            or current_conversation.draft["thinking_content"]
                            or current_conversation.draft["tool_calls"]
                        )
                        and (
                            delta["role"]
                            or (  # for last content, only commit if it exists in the chat log
                                chat_log.content[-1].role == "assistant"
                                and chat_log.content[-1].content.endswith(
                                    current_conversation.draft["content"]
                                )  # endswith is to cater for a possible reaction
                            )
                        )
                    ):
                        await self.async_handle_chat_log_event(
                            thread_id=thread_id,
                            current_conversation=current_conversation,
                            event_type=ChatLogEventType.CONTENT_ADDED,
                            data={"content": current_conversation.draft},
                            context=context,
                        )
                if delta["role"] == "assistant":
                    current_conversation.draft = AssistantContentDeltaDict(
                        role="assistant",
                        content="",
                        thinking_content="",
                        tool_calls=[],
                    )
                else:
                    current_conversation.draft = None

                if delta["role"] is None:
                    responded_tool_calls: set[str] = set()
                    async with current_conversation.content_lock:
                        for content in reversed(chat_log.content):
                            if content.role == "tool_result":
                                responded_tool_calls.add(content.tool_call_id)
                                continue
                            if content.role == "assistant":
                                for tool_call in content.tool_calls or []:
                                    if tool_call.id not in responded_tool_calls:
                                        chat_log.async_add_assistant_content_without_tools(
                                            ToolResultContent(
                                                agent_id=self.agent_id,
                                                tool_call_id=tool_call.id,
                                                tool_name=tool_call.tool_name,
                                                tool_result={
                                                    "error": "asyncio.CancelledError: "
                                                    "Conversation interrupted before "
                                                    "tool call could be responded to. "
                                                    "Please try again."
                                                },
                                            )
                                        )
                            break

                if current_conversation.draft:
                    # Send typing action at the beginning of each assistant response
                    await self.hass.services.async_call(
                        TELEGRAM_DOMAIN,
                        SERVICE_SEND_CHAT_ACTION,
                        {
                            CONF_CONFIG_ENTRY_ID: self.telegram_entry_id,
                            **get_telegram_service_target(
                                self.chat_id,
                                self.notify_entity_id,
                            ),
                            ATTR_MESSAGE_THREAD_ID: thread_id,
                            ATTR_CHAT_ACTION: CHAT_ACTION_TYPING,
                        },
                        context=context,
                    )

            if current_conversation.draft:
                if "content" in delta:
                    if not current_conversation.draft["content"] and (
                        reaction := get_reaction(delta["content"])
                    ):
                        await self.hass.services.async_call(
                            TELEGRAM_DOMAIN,
                            SERVICE_SET_MESSAGE_REACTION,
                            {
                                CONF_CONFIG_ENTRY_ID: self.telegram_entry_id,
                                ATTR_MESSAGE_ID: msg_id or "last",
                                ATTR_CHAT_ID: self.chat_id,
                                ATTR_REACTION: reaction,
                            },
                            context=context,
                        )
                        current_conversation.draft["content"] = (
                            delta["content"].lstrip().removeprefix(reaction).lstrip()
                        )
                    else:
                        current_conversation.draft["content"] += delta["content"]
                if "thinking_content" in delta:
                    current_conversation.draft["thinking_content"] += delta[
                        "thinking_content"
                    ]
                if "tool_calls" in delta:
                    current_conversation.draft["tool_calls"].extend(delta["tool_calls"])

    async def async_handle_chat_log_event(
        self,
        thread_id: int,
        current_conversation: ConversationConfig,
        event_type: ChatLogEventType,
        data: dict[str, Any],
        context: Context | None = None,
    ) -> None:
        """Handle chat log events."""
        async with current_conversation.content_lock:
            if (
                event_type == ChatLogEventType.CONTENT_ADDED
                and (content := data.get("content"))
                and content.get("role") == "assistant"
                and (message := content.get("content"))
            ):
                await self.send_message(
                    message=message,
                    message_thread_id=thread_id,
                    context=context,
                )

    async def async_change_agent(self, agent_id: str) -> bool:
        """Change the conversation agent for this chat."""
        LOGGER.debug(
            "Change agent for chat_id=%s to agent_id=%s", self.chat_id, agent_id
        )

        self.agent_id = agent_id
        # The above might be redundant because we are reloading the config entry
        subentry = self.entry.subentries[self.subentry_id]
        data = {**subentry.data, CONF_CONVERSATION_AGENT: agent_id}
        LOGGER.debug("Updating subentry %s with data %s", subentry.subentry_id, data)
        try:
            self.hass.config_entries.async_update_subentry(
                self.entry, subentry, data=data
            )
        except HomeAssistantError as e:
            LOGGER.exception(
                "Failed to update subentry %s: %s",
                subentry.subentry_id,
                e,
                stack_info=True,
            )
            return False
        LOGGER.debug("Subentry %s updated", subentry.subentry_id)

        return True

    async def async_process_command(
        self,
        message_thread_id: int,
        command: str,
        args: list[str],
        context: Context,
    ) -> None:
        """Process a bot command."""
        LOGGER.debug("Received command: %s with args: %s", command, args)
        match command:
            case "/model":
                selected_agent = args[0] if len(args) > 0 else None
                current_agent = self.agent_id

                agents = {
                    agent.id: agent.name
                    for agent in get_agent_manager(self.hass).async_get_agent_info()
                    if not isinstance(
                        get_agent_manager(self.hass).async_get_agent(agent.id),
                        ConversationEntity,
                    )
                } | {
                    entity.entity_id: (
                        self.hass.states.get(entity.entity_id).name
                        if self.hass.states.get(entity.entity_id)
                        else entity.entity_id
                    )
                    for entity in self.hass.data[DATA_COMPONENT].entities
                }

                LOGGER.debug(
                    "Selected agent: %s, current agent: %s, available agents: %s",
                    selected_agent,
                    current_agent,
                    agents,
                )
                if (
                    selected_agent is not None
                    and selected_agent in agents
                    and await self.async_change_agent(selected_agent)
                ):
                    LOGGER.debug(
                        "Agent switched to %s for chat_id=%s",
                        selected_agent,
                        self.chat_id,
                    )
                    await self.send_message(
                        message=f"Conversation agent switched to `{
                            agents.get(selected_agent, selected_agent)
                        }`",
                        message_thread_id=message_thread_id,
                        context=context,
                    )
                else:
                    messages = await self.send_message(
                        message=f"Current conversation agent: `{
                            agents.get(current_agent, current_agent)
                        }`",
                        message_thread_id=message_thread_id,
                        context=context,
                    )
                    if messages["chats"]:
                        msg = messages["chats"][-1]
                        await self.hass.services.async_call(
                            TELEGRAM_DOMAIN,
                            SERVICE_EDIT_REPLYMARKUP,
                            {
                                CONF_CONFIG_ENTRY_ID: self.telegram_entry_id,
                                **get_telegram_service_target(
                                    msg[ATTR_CHAT_ID],
                                    self.notify_entity_id,
                                ),
                                ATTR_MESSAGE_ID: msg[ATTR_MESSAGE_ID],
                                ATTR_KEYBOARD_INLINE: [
                                    [(agent_name, f"/model {agent_id}")]
                                    for agent_id, agent_name in agents.items()
                                ],
                            },
                            context=context,
                        )
            case "/new":
                if (
                    (current_conversation := self.conversations.get(message_thread_id))
                    and (task := current_conversation.task)
                    and not task.done()
                ):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

                self.conversations.pop(message_thread_id, None)
                await self.send_message(
                    message="New conversation started.",
                    message_thread_id=message_thread_id,
                    context=context,
                )

    async def async_handle_command(self, event: Event) -> None:
        """Handle command events."""
        context = event.context
        if context.user_id is None:
            context.user_id = self.user_id
        try:
            await self.async_process_command(
                event.data.get(ATTR_MESSAGE_THREAD_ID) or 0,
                event.data["command"],
                event.data.get("args", []),
                context,
            )
        except Exception as e:  # noqa: BLE001
            LOGGER.exception("Failed to process command: %s", e, stack_info=True)

    async def async_handle_callback(self, event: Event) -> None:
        """Handle callback query events."""
        LOGGER.debug("callback_event data: %s", event.data)
        context = event.context
        if context.user_id is None:
            context.user_id = self.user_id
        args = event.data.get("data", "").split(" ")

        try:
            await self.async_process_command(
                event.data.get(ATTR_MESSAGE, {}).get(ATTR_MESSAGE_THREAD_ID) or 0,
                args.pop(0),
                args,
                context,
            )
            await self.hass.services.async_call(
                TELEGRAM_DOMAIN,
                SERVICE_ANSWER_CALLBACK_QUERY,
                {
                    CONF_CONFIG_ENTRY_ID: self.telegram_entry_id,
                    ATTR_MESSAGE: "Done",
                    ATTR_CALLBACK_QUERY_ID: event.data.get(ATTR_MSGID),
                },
                context=context,
            )
            if event.data[ATTR_MSG]:
                await self.hass.services.async_call(
                    TELEGRAM_DOMAIN,
                    SERVICE_EDIT_REPLYMARKUP,
                    {
                        CONF_CONFIG_ENTRY_ID: self.telegram_entry_id,
                        **get_telegram_service_target(
                            self.chat_id,
                            self.notify_entity_id,
                        ),
                        ATTR_MESSAGE_ID: event.data[ATTR_MSG][ATTR_MESSAGE_ID],
                        ATTR_KEYBOARD_INLINE: [],
                    },
                    context=context,
                )

        except Exception as e:  # noqa: BLE001
            LOGGER.exception("Failed to process command: %s", e, stack_info=True)
