# AGENTS.md

This file provides guidance to AI coding agents when working with code in this repository.

## Project Overview

Home Assistant custom integration that bridges the Telegram Bot integration with Conversation agents (OpenAI, Anthropic Claude, Google Gemini, etc.). Installed via HACS.

## Development Commands

**Setup:**

```bash
scripts/bootstrap          # Install deps, setup venv, install pre-commit hooks
scripts/bootstrap --full   # Full setup including all HA test dependencies
```

**Testing:**

```bash
scripts/run-in-env.sh pytest                     # Run all tests
scripts/run-in-env.sh pytest tests/test_entity.py                    # Run a single test file
scripts/run-in-env.sh pytest tests/test_entity.py::test_function     # Run a single test
scripts/run-in-env.sh pytest --cov custom_components/telegram_bot_conversation --cov-report=term-missing  # With coverage
```

Tests use `pytest-homeassistant-custom-component` and `pytest-asyncio` with `asyncio_mode = "auto"` (no need for `@pytest.mark.asyncio` decorators).

**Linting & Formatting:**

```bash
scripts/lint               # Run ruff format + ruff check --fix (auto-fixes)
scripts/lint-check         # Check only, no auto-fix
prek run --all-files       # Run all pre-commit hooks (ruff, codespell, yaml, json, etc.)
```

**Type checking:**

```bash
uv run mypy custom_components/telegram_bot_conversation
```

## Architecture

### Core Components

**`__init__.py` — `TelegramBotConversationHandler`**

- Main handler managing the integration lifecycle
- Maintains `chat_handlers` dict mapping `chat_id → TelegramChatHandler`
- Registers listeners for Telegram events: `EVENT_TELEGRAM_TEXT`, `EVENT_TELEGRAM_ATTACHMENT`, `EVENT_TELEGRAM_COMMAND`, `EVENT_TELEGRAM_CALLBACK`
- Filters events by `telegram_entry_id` and configured `chat_id`
- Entry points: `async_setup_entry()`, `async_unload_entry()`, `async_migrate_entry()`

**`entity.py` — `TelegramChatHandler`**

- Per-chat conversation state and message handling (the largest file)
- Handles text messages, commands (`/model`, `/new`), callbacks, and streaming responses
- Manages draft message updates during streaming with multiple asyncio locks (`delta`, `content`, `send`)
- Message formatting pipeline: Markdown → Telegram MarkdownV2 (via `telegramify_markdown`), code block → file attachment, Mermaid → image, LaTeX → Unicode
- Splits messages at Telegram's 4096 character limit

**`config_flow.py` — `TelegramBotConversationFlow`**

- Config flow with subentry support for per-chat configuration
- Built on `RecursiveConfigFlow` (in `recursive_data_flow.py`) — a reusable framework for hierarchical Home Assistant config flows with recursive subentry creation

**`intent.py` — Intent Handlers**

- `GenerateImageHandler` for "TelegramGenerateImage" intent via HA AI Task
- Smart context resolution to find the correct chat when called outside Telegram context

**`const.py`** — Constants, config keys, and the 64-emoji reaction set

### Data Flow

```
Telegram message → EVENT_TELEGRAM_* → TelegramBotConversationHandler (filter + route)
  → TelegramChatHandler.async_handle_text() → async_get_chat_session() + async_converse()
  → Streaming response with draft updates → telegramify + formatting → telegram_bot.send_message
```

### Key Patterns

- **Subentry-based config**: Each Telegram chat has its own subentry with separate conversation agent, HA user mapping, timeout, and feature toggles
- **Streaming with drafts**: AI responses stream in real-time; `TelegramChatHandler` edits the draft message as content arrives, using per-thread `ConversationConfig` dataclass with multiple locks for concurrency
- **Thread/topic support**: `conversations` dict keyed by thread_id for partial Telegram topic support

### Test Structure

Tests in `tests/` mirror source modules: `test_init.py`, `test_config_flow.py`, `test_entity.py`, `test_intent.py`. Fixtures in `conftest.py` provide mocked HA instances, Telegram bot/application, and config entries. The test framework is `pytest-homeassistant-custom-component` which supplies HA-specific fixtures like `hass`.
