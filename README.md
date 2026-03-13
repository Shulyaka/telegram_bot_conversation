# Telegram Bot Conversation

This is a Home Assistant custom integration that allows you to use the [Telegram bot](https://www.home-assistant.io/integrations/telegram_bot/) integration with [Conversation](https://www.home-assistant.io/integrations/conversation/) agents such as [OpenAI](https://www.home-assistant.io/integrations/openai_conversation/), [Anthropic Claude](https://www.home-assistant.io/integrations/anthropic/), or [Google Gemini](https://www.home-assistant.io/integrations/google_generative_ai_conversation/).

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?category=integration&owner=Shulyaka&repository=telegram_bot_conversation)

## Requirements

- Home Assistant 2026.3 or later
- Telegram Bot integration set up and working
- Any conversation AI integration is recommended, however the NLP Assist works too.

## Features

- Per-chat configuration of HA User mapping and conversation agent
- Extended conversation history timeout
- Easy switch between agents with `/model` command
- Start a conversation from scratch with `/new` command
- Full Markdown support
- Code blocks can be sent as files, mermaid diagrams can be rendered into images, and LaTeX symbols can be translated into Unicode
- Attachments are supported if your agent supports them (usually only images and pdf, and only for the last message)
- Partial support for threaded bots (awaiting `telegram_bot` for full support)
