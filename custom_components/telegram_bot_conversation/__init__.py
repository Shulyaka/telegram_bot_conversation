"""Custom integration to integrate telegram_bot_conversation with Home Assistant.

This integration ties up the Home Assistant conversation
integration with the telegram_bot integration.

Requires telegram_bot to be set up.

Credits: https://gist.github.com/balloob/d59cae89d19a14bcec99ce1bde05bd44

For more details about this integration, please refer to
https://github.com/Shulyaka/telegram_bot_conversation
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
import tempfile
from typing import Any, Self

from telegramify_markdown import telegramify
from telegramify_markdown.interpreters import (
    FileInterpreter,
    MermaidInterpreter,
    TextInterpreter,
)
from telegramify_markdown.type import ContentTypes

from homeassistant.components.conversation import (
    DATA_COMPONENT,
    Attachment,
    ChatLog,
    ConversationEntity,
    UserContent,
    async_converse,
    async_get_chat_log,
    get_agent_manager,
)
from homeassistant.components.notify import DOMAIN as NOTIFY_DOMAIN
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
    ATTR_USER_ID,
    CHAT_ACTION_TYPING,
    CONF_CHAT_ID,
    CONF_CONFIG_ENTRY_ID,
    DOMAIN as TELEGRAM_DOMAIN,
    EVENT_TELEGRAM_ATTACHMENT,
    EVENT_TELEGRAM_CALLBACK,
    EVENT_TELEGRAM_COMMAND,
    EVENT_TELEGRAM_SENT,
    EVENT_TELEGRAM_TEXT,
    SERVICE_ANSWER_CALLBACK_QUERY,
    SERVICE_DOWNLOAD_FILE,
    SERVICE_EDIT_REPLYMARKUP,
    SERVICE_SEND_CHAT_ACTION,
    SERVICE_SEND_DOCUMENT,
    SERVICE_SEND_MESSAGE,
    SERVICE_SEND_PHOTO,
    SERVICE_SET_MESSAGE_REACTION,
    SUBENTRY_TYPE_ALLOWED_CHAT_IDS,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
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
    CONF_TELEGRAM_ENTRY,
    CONF_TELEGRAM_SUBENTRY,
    CONF_TMPDIR,
    CONF_USER,
    DEFAULT_CONVERSATION_TIMEOUT,
    LOGGER,
    REACTION_EMOJI,
)

type TelegramBotConversationConfigEntry = ConfigEntry[None]

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
    def callback_sent_filter(self, event_data: dict[str, Any]) -> bool:
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
class ChatConfig:
    """Per-chat configuration resolved from config entry subentries."""

    user_id: str | None
    agent_id: str | None
    notify_entity_id: str | None
    subentry_id: str


class TelegramBotConversationHandler:
    """Handle Telegram bot conversation events."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: TelegramBotConversationConfigEntry,
    ) -> None:
        """Initialize the handler."""
        self.hass = hass
        self.entry = entry
        self.telegram_entry_id = entry.data.get(CONF_TELEGRAM_ENTRY)

        options = self.entry.options
        self.interpreter_chain = [TextInterpreter()]
        self.extra_prompt = (
            "The user is interacting through Telegram. Markdown is supported. "
        )
        if options.get(CONF_ATTACHMENTS):
            self.interpreter_chain.append(FileInterpreter())
            self.extra_prompt += "Code blocks will be sent as files. "
        if options.get(CONF_MERMAID):
            self.interpreter_chain.append(MermaidInterpreter())
            self.extra_prompt += "Mermaid is supported as inline code blocks. "
        self.extra_prompt += (
            f"If the response message starts with any of {REACTION_EMOJI}, "
            "it will be added as a reaction to the user message."
        )

        telegram_entry = hass.config_entries.async_get_entry(self.telegram_entry_id)

        # TODO(@Shulyaka): Check if a telegram subentry has been deleted and raise
        # a repair issue

        linked_telegram_subentries = {
            s.data[CONF_TELEGRAM_SUBENTRY]
            for s in entry.subentries.values()
            if s.subentry_type == "telegram_id"
        }

        telegram_id_map = {
            subentry_id: subentry.data[CONF_CHAT_ID]
            for subentry_id, subentry in telegram_entry.subentries.items()
            if subentry.subentry_type == SUBENTRY_TYPE_ALLOWED_CHAT_IDS
            and subentry.data.get(CONF_CHAT_ID) is not None
            and subentry.subentry_id in linked_telegram_subentries
        }
        entity_registry = er.async_get(hass)
        telegram_notify_map = {
            entity_entry.config_subentry_id: entity_entry.entity_id
            for entity_entry in er.async_entries_for_config_entry(
                entity_registry, telegram_entry.entry_id
            )
            if entity_entry.config_subentry_id in linked_telegram_subentries
            and entity_entry.domain == NOTIFY_DOMAIN
        }

        self.chat_config: dict[int, ChatConfig] = {}
        for subentry_id, subentry in entry.subentries.items():
            if subentry.subentry_type != "telegram_id":
                continue
            tg_sub = subentry.data.get(CONF_TELEGRAM_SUBENTRY)
            if tg_sub not in telegram_id_map:
                continue
            chat_id = telegram_id_map[tg_sub]
            self.chat_config[chat_id] = ChatConfig(
                user_id=subentry.data.get(CONF_USER),
                agent_id=subentry.data.get(CONF_CONVERSATION_AGENT),
                notify_entity_id=telegram_notify_map.get(tg_sub),
                subentry_id=subentry_id,
            )

        self._register_listeners()

    def _register_listeners(self) -> None:
        """Register all event listeners."""
        self.entry.async_on_unload(
            self.hass.bus.async_listen(
                EVENT_TELEGRAM_TEXT,
                self.async_handle_text,
                self.text_events_filter,
            )
        )

        self.entry.async_on_unload(
            self.hass.bus.async_listen(
                EVENT_TELEGRAM_ATTACHMENT,
                self.async_handle_text,
                self.text_events_filter,
            )
        )

        self.entry.async_on_unload(
            self.hass.bus.async_listen(
                EVENT_TELEGRAM_COMMAND,
                self.async_handle_command,
                self.command_events_filter,
            )
        )

        self.entry.async_on_unload(
            self.hass.bus.async_listen(
                EVENT_TELEGRAM_CALLBACK,
                self.async_handle_callback,
                self.callback_events_filter,
            )
        )

    async def send_message(
        self,
        hass: HomeAssistant,
        chat_id: int,
        message_thread_id: int,
        message: str,
        context: Context | None = None,
    ) -> dict[str, list[dict[str, int]]]:
        """Send telegram message, taking care of formatting and length."""
        if context is None:
            context = Context()

        messages = {"chats": []}
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
            with TelegramMessageWatcher(hass, self.telegram_entry_id) as watcher:
                for item in await telegramify(
                    content=message,
                    interpreters_use=self.interpreter_chain,
                    normalize_whitespace=True,
                    latex_escape=self.entry.options.get(CONF_LATEX, True),
                    max_word_count=MAX_TELEGRAM_LENGTH,
                ):
                    if item.content_type == ContentTypes.TEXT:
                        item_messages = await hass.services.async_call(
                            TELEGRAM_DOMAIN,
                            SERVICE_SEND_MESSAGE,
                            {
                                ATTR_MESSAGE: item.content,
                                **get_telegram_service_target(
                                    chat_id, self.chat_config[chat_id].notify_entity_id
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
                        await asyncio.gather(
                            *(
                                watcher.wait_message(
                                    msg[ATTR_CHAT_ID], msg[ATTR_MESSAGE_ID]
                                )
                                for msg in item_messages["chats"]
                            )
                        )
                    elif item.content_type in (ContentTypes.PHOTO, ContentTypes.FILE):
                        item_messages = await hass.services.async_call(
                            TELEGRAM_DOMAIN,
                            SERVICE_SEND_PHOTO
                            if item.content_type == ContentTypes.PHOTO
                            else SERVICE_SEND_DOCUMENT,
                            {
                                ATTR_FILE: (
                                    await hass.async_add_executor_job(
                                        save_file, item.file_name, item.file_data
                                    )
                                ).as_posix(),
                                ATTR_CAPTION: item.caption,
                                **get_telegram_service_target(
                                    chat_id, self.chat_config[chat_id].notify_entity_id
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
                        await asyncio.gather(
                            *(
                                watcher.wait_message(
                                    msg[ATTR_CHAT_ID], msg[ATTR_MESSAGE_ID]
                                )
                                for msg in item_messages["chats"]
                            )
                        )
        finally:
            if created_files:
                hass.async_add_executor_job(
                    lambda: [file.unlink(missing_ok=True) for file in created_files]
                )

        return messages

    async def async_handle_text(self, event: Event) -> None:
        """Handle text and attachment events."""
        conversation_id = f"telegram_{event.data[ATTR_CHAT_ID]}"
        if event.data.get(ATTR_MESSAGE_THREAD_ID) is not None:
            conversation_id += f"_{event.data[ATTR_MESSAGE_THREAD_ID]}"
        context = event.context
        chat_config = self.chat_config[event.data[ATTR_CHAT_ID]]
        if context.user_id is None:
            context.user_id = chat_config.user_id

        current_content = ""
        current_role = None

        def get_reaction(content: str) -> str | None:
            """Extract reaction from content if it starts with a known reaction emoji."""
            matches = [e for e in REACTION_EMOJI if content.lstrip().startswith(e)]
            return max(matches, key=len) if matches else None

        async def async_chat_log_delta_listener(chat_log: ChatLog, delta: dict) -> None:
            """Handle chat log delta."""
            LOGGER.debug("Chat log delta: %s", delta)
            nonlocal current_content, current_role
            if "role" in delta:
                await self.hass.services.async_call(
                    TELEGRAM_DOMAIN,
                    SERVICE_SEND_CHAT_ACTION,
                    {
                        CONF_CONFIG_ENTRY_ID: self.telegram_entry_id,
                        **get_telegram_service_target(
                            event.data[ATTR_CHAT_ID],
                            chat_config.notify_entity_id,
                        ),
                        ATTR_MESSAGE_THREAD_ID: event.data.get(ATTR_MESSAGE_THREAD_ID)
                        or 0,
                        ATTR_CHAT_ACTION: CHAT_ACTION_TYPING,
                    },
                    context=context,
                )

                if current_role == "assistant" and current_content:
                    await self.send_message(
                        self.hass,
                        chat_id=event.data[ATTR_CHAT_ID],
                        message_thread_id=event.data.get(ATTR_MESSAGE_THREAD_ID) or 0,
                        message=current_content,
                        context=context,
                    )
                current_content = ""
                current_role = delta["role"]
            if "content" in delta and current_role == "assistant":
                if not current_content and (reaction := get_reaction(delta["content"])):
                    await self.hass.services.async_call(
                        TELEGRAM_DOMAIN,
                        SERVICE_SET_MESSAGE_REACTION,
                        {
                            CONF_CONFIG_ENTRY_ID: self.telegram_entry_id,
                            ATTR_MESSAGE_ID: event.data.get(ATTR_MSGID) or "last",
                            ATTR_CHAT_ID: event.data[ATTR_CHAT_ID],
                            ATTR_REACTION: reaction,
                        },
                        context=context,
                    )
                    current_content = (
                        delta["content"].lstrip().removeprefix(reaction).lstrip()
                    )
                else:
                    current_content += delta["content"]

        @callback
        def chat_log_delta_listener(chat_log: ChatLog, delta: dict) -> None:
            """Handle chat log delta."""
            self.hass.async_create_task(
                async_chat_log_delta_listener(chat_log, delta),
                "async_chat_log_delta_listener",
            )

        with (
            async_get_chat_session(self.hass, conversation_id) as session,
            async_get_chat_log(
                self.hass,
                session,
                chat_log_delta_listener=chat_log_delta_listener,
            ) as chat_log,
        ):
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

            conversation_result = await async_converse(
                self.hass,
                text=input_text,
                conversation_id=session.conversation_id,
                context=context,
                agent_id=self.chat_config[event.data[ATTR_CHAT_ID]].agent_id,
                extra_system_prompt=self.extra_prompt,
            )
            # Flush any remaining delta
            chat_log_delta_listener(chat_log, {"role": None})

        timeout = self.entry.options.get(
            CONF_CONVERSATION_TIMEOUT,
            {"seconds": DEFAULT_CONVERSATION_TIMEOUT.total_seconds()},
        )
        session.last_updated = (
            dt_util.utcnow() + timedelta(**timeout) - CONVERSATION_TIMEOUT
        )

        if conversation_result.response.response_type == IntentResponseType.ERROR:
            await self.send_message(
                self.hass,
                chat_id=event.data[ATTR_CHAT_ID],
                message_thread_id=event.data.get(ATTR_MESSAGE_THREAD_ID) or 0,
                message=conversation_result.response.speech["plain"]["speech"],
                context=context,
            )

    @callback
    def text_events_filter(self, event_data: dict[str, Any]) -> bool:
        """Filter text and attachment events."""
        return (
            event_data.get("bot", {}).get(CONF_CONFIG_ENTRY_ID)
            == self.telegram_entry_id
            and event_data.get(ATTR_CHAT_ID) in self.chat_config
            and event_data.get(ATTR_CHAT_ID) == event_data.get(ATTR_USER_ID)
        )

    async def async_change_agent(self, chat_id: int, agent_id: str) -> bool:
        """Change the conversation agent for a chat."""
        LOGGER.debug("Change agent for chat_id=%s to agent_id=%s", chat_id, agent_id)
        if chat_id not in self.chat_config:
            return False

        self.chat_config[chat_id].agent_id = agent_id
        # The above might be redundant because we are reloading the config entry
        subentry = self.entry.subentries.get(self.chat_config[chat_id].subentry_id)
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
        chat_id: int,
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
                current_agent = self.chat_config[chat_id].agent_id

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
                    and chat_id in self.chat_config
                    and await self.async_change_agent(chat_id, selected_agent)
                ):
                    LOGGER.debug(
                        "Agent switched to %s for chat_id=%s",
                        selected_agent,
                        chat_id,
                    )
                    await self.send_message(
                        self.hass,
                        chat_id=chat_id,
                        message_thread_id=message_thread_id,
                        message=f"Conversation agent switched to `{
                            agents.get(selected_agent, selected_agent)
                        }`",
                        context=context,
                    )
                else:
                    messages = await self.send_message(
                        self.hass,
                        chat_id=chat_id,
                        message_thread_id=message_thread_id,
                        message=f"Current conversation agent: `{
                            agents.get(current_agent, current_agent)
                        }`",
                        context=context,
                    )
                    if messages["chats"]:
                        msg = messages["chats"][-1]
                        config = self.chat_config[msg[ATTR_CHAT_ID]]
                        await self.hass.services.async_call(
                            TELEGRAM_DOMAIN,
                            SERVICE_EDIT_REPLYMARKUP,
                            {
                                CONF_CONFIG_ENTRY_ID: self.telegram_entry_id,
                                **get_telegram_service_target(
                                    msg[ATTR_CHAT_ID],
                                    config.notify_entity_id,
                                ),
                                ATTR_MESSAGE_ID: msg[ATTR_MESSAGE_ID],
                                ATTR_KEYBOARD_INLINE: [
                                    [(agent_name, f"/model {agent_id}")]
                                    for agent_id, agent_name in agents.items()
                                ],
                            },
                            context=context,
                        )

    async def async_handle_command(self, event: Event) -> None:
        """Handle command events."""
        context = event.context
        if context.user_id is None:
            config = self.chat_config[event.data[ATTR_CHAT_ID]]
            context.user_id = config.user_id
        try:
            await self.async_process_command(
                event.data[ATTR_CHAT_ID],
                event.data.get(ATTR_MESSAGE_THREAD_ID) or 0,
                event.data.get("command"),
                event.data.get("args", []),
                context,
            )
        except Exception as e:  # noqa: BLE001
            LOGGER.exception("Failed to process command: %s", e, stack_info=True)

    @callback
    def command_events_filter(self, event_data: dict[str, Any]) -> bool:
        """Filter command events."""
        return (
            event_data.get("command") == "/model"
            and event_data.get("bot", {}).get(CONF_CONFIG_ENTRY_ID)
            == self.telegram_entry_id
            and event_data.get(ATTR_CHAT_ID) in self.chat_config
            and event_data.get(ATTR_CHAT_ID) == event_data.get(ATTR_USER_ID)
        )

    async def async_handle_callback(self, event: Event) -> None:
        """Handle callback query events."""
        LOGGER.debug("callback_event data: %s", event.data)
        context = event.context
        config = self.chat_config[event.data[ATTR_CHAT_ID]]
        if context.user_id is None:
            context.user_id = config.user_id
        args = event.data.get("data", "").split(" ")

        try:
            await self.async_process_command(
                event.data[ATTR_CHAT_ID],
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
                            event.data[ATTR_CHAT_ID],
                            config.notify_entity_id if config else None,
                        ),
                        ATTR_MESSAGE_ID: event.data[ATTR_MSG][ATTR_MESSAGE_ID],
                        ATTR_KEYBOARD_INLINE: [],
                    },
                    context=context,
                )

        except Exception as e:  # noqa: BLE001
            LOGGER.exception("Failed to process command: %s", e, stack_info=True)

    @callback
    def callback_events_filter(self, event_data: dict[str, Any]) -> bool:
        """Filter callback query events."""
        return (
            event_data.get("data", "").startswith("/model")
            and event_data.get("bot", {}).get(CONF_CONFIG_ENTRY_ID)
            == self.telegram_entry_id
            and event_data.get(ATTR_CHAT_ID) in self.chat_config
            and event_data.get(ATTR_CHAT_ID) == event_data.get(ATTR_USER_ID)
        )


async def async_setup_entry(
    hass: HomeAssistant, entry: TelegramBotConversationConfigEntry
) -> bool:
    """Set up this integration using UI."""
    TelegramBotConversationHandler(hass, entry)

    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: TelegramBotConversationConfigEntry
) -> bool:
    """Unload Telegram Bot Conversation."""
    return True


async def async_update_options(
    hass: HomeAssistant, entry: TelegramBotConversationConfigEntry
) -> None:
    """Update options."""
    await hass.config_entries.async_reload(entry.entry_id)
