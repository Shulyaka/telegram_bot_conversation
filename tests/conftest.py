"""Tests helpers."""

from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from telegram import AcceptedGiftTypes, Bot, Chat, ChatFullInfo, Message, User
from telegram.constants import ChatType

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
from homeassistant.components.telegram_bot.const import (
    ATTR_PARSER,
    CONF_ALLOWED_CHAT_IDS,
    CONF_API_ENDPOINT,
    CONF_CHAT_ID,
    DEFAULT_API_ENDPOINT,
    DOMAIN as TELEGRAM_DOMAIN,
    PARSER_MD,
    PLATFORM_POLLING,
)
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import CONF_API_KEY, CONF_PLATFORM
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component


# This fixture enables loading custom integrations in all tests.
# Remove to enable selective use of this fixture
@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations."""
    return


# This fixture is used to prevent HomeAssistant from attempting to create and dismiss
# persistent notifications. These calls would fail without this fixture since the
# persistent_notification integration is never loaded during a test.
@pytest.fixture(name="skip_notifications", autouse=True)
def skip_notifications_fixture():
    """Skip notification calls."""
    with (
        patch("homeassistant.components.persistent_notification.async_create"),
        patch("homeassistant.components.persistent_notification.async_dismiss"),
    ):
        yield


@pytest.fixture
def mock_telegram_calls() -> Generator[None]:
    """Fixture for setting up the polling platform using appropriate config and mocks."""
    with patch(
        "homeassistant.components.telegram_bot.polling.ApplicationBuilder"
    ) as application_builder_class:
        application = (
            application_builder_class.return_value.bot.return_value.build.return_value
        )
        application.initialize = AsyncMock()
        application.updater.start_polling = AsyncMock()
        application.start = AsyncMock()
        application.updater.stop = AsyncMock()
        application.stop = AsyncMock()
        application.shutdown = AsyncMock()

        yield


@pytest.fixture
def mock_telegram_external_calls() -> Generator[None]:
    """Mock calls that make calls to the live Telegram API."""
    test_chat = ChatFullInfo(
        id=123456,
        title="mock title",
        first_name="mock first_name",
        type="PRIVATE",
        max_reaction_count=100,
        accent_color_id=0,
        accepted_gift_types=AcceptedGiftTypes(True, True, True, True),
    )
    test_user = User(123456, "Testbot", True, "mock last name", "mock username")
    message = Message(
        message_id=12345,
        date=datetime.now(UTC),
        chat=Chat(id=123456, type=ChatType.PRIVATE),
    )

    class BotMock(Bot):
        """Mock bot class."""

        __slots__ = ()

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """Initialize BotMock instance."""
            super().__init__(*args, **kwargs)
            self._bot_user = test_user

    with (
        patch("homeassistant.components.telegram_bot.bot.Bot", BotMock),
        patch.object(BotMock, "get_chat", return_value=test_chat),
        patch.object(BotMock, "get_me", return_value=test_user),
        patch.object(BotMock, "bot", test_user),
        patch.object(BotMock, "send_message", return_value=message),
        patch.object(BotMock, "send_photo", return_value=message),
        patch.object(BotMock, "send_sticker", return_value=message),
        patch.object(BotMock, "send_video", return_value=message),
        patch.object(BotMock, "send_document", return_value=message),
        patch.object(BotMock, "send_voice", return_value=message),
        patch.object(BotMock, "send_animation", return_value=message),
        patch.object(BotMock, "send_location", return_value=message),
        patch.object(BotMock, "send_poll", return_value=message),
        patch.object(BotMock, "log_out", return_value=True),
        patch("telegram.ext.Updater._bootstrap"),
    ):
        yield


@pytest.fixture
async def mock_telegram_config_entry(
    hass, mock_telegram_calls, mock_telegram_external_calls
) -> MockConfigEntry:
    """Return the default mocked config entry."""
    entry = MockConfigEntry(
        unique_id="mock api key",
        domain=TELEGRAM_DOMAIN,
        data={
            CONF_PLATFORM: PLATFORM_POLLING,
            CONF_API_KEY: "mock api key",
            CONF_API_ENDPOINT: DEFAULT_API_ENDPOINT,
        },
        options={ATTR_PARSER: PARSER_MD},
        subentries_data=[
            ConfigSubentryData(
                unique_id="1234567890",
                data={CONF_CHAT_ID: 12345678},
                subentry_type=CONF_ALLOWED_CHAT_IDS,
                title="mock chat 1",
            ),
            ConfigSubentryData(
                unique_id="1234567891",
                data={CONF_CHAT_ID: -123456789},
                subentry_type=CONF_ALLOWED_CHAT_IDS,
                title="mock chat 2",
            ),
        ],
        minor_version=2,
    )

    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    yield entry

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
async def mock_config_entry(
    hass, hass_config_dir: str, mock_telegram_config_entry
) -> MockConfigEntry:
    """Mock a config entry."""

    for telegram_subentry in mock_telegram_config_entry.subentries.values():
        await hass.auth.async_create_user(name=telegram_subentry.title)

    async def get_user_id(name: str) -> str | None:
        for user in await hass.auth.async_get_users():
            if user.name == name and not user.system_generated:
                return user.id
        return None

    entry = MockConfigEntry(
        title=mock_telegram_config_entry.title,
        domain=DOMAIN,
        data={CONF_TELEGRAM_ENTRY: mock_telegram_config_entry.entry_id},
        options={
            CONF_CONVERSATION_TIMEOUT: {"minutes": 15},
            CONF_ATTACHMENTS: 15,
            CONF_LATEX: False,
            CONF_MERMAID: False,
            CONF_DISABLE_WEB_PREV: True,
            CONF_THOUGHTS: False,
            CONF_TMPDIR: hass_config_dir + "/www",
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
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    yield entry

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture(autouse=True)
async def setup_ha(hass: HomeAssistant) -> None:
    """Set up Home Assistant."""
    hass.config.allowlist_external_dirs = {
        hass.config.path("www"),
        *hass.config.media_dirs.values(),
    }
    assert await async_setup_component(hass, "homeassistant", {})
