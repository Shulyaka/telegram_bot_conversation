"""Tests for integration services."""

import json
from pathlib import Path
from typing import Any

import yaml

from custom_components.telegram_bot_conversation import SERVICE_MARKDOWNIFY
from custom_components.telegram_bot_conversation.const import DOMAIN
from homeassistant.components.telegram_bot.const import (
    ATTR_PARSER,
    ATTR_TEXT,
    PARSER_PLAIN_TEXT,
)
from homeassistant.core import HomeAssistant

ROOT = Path(__file__).parents[1]
TRANSLATIONS_DIR = ROOT / "custom_components/telegram_bot_conversation/translations"
SERVICES_YAML = ROOT / "custom_components/telegram_bot_conversation/services.yaml"


def _leaf_paths(value: Any, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Return all leaf paths in a nested translation mapping."""
    if not isinstance(value, dict):
        return {prefix}

    paths: set[tuple[str, ...]] = set()
    for key, item in value.items():
        paths.update(_leaf_paths(item, (*prefix, key)))
    return paths


def _load_translation(language: str) -> dict[str, Any]:
    """Load a translation file."""
    return json.loads((TRANSLATIONS_DIR / f"{language}.json").read_text())


async def test_markdownify_service_strips_markdown(
    hass: HomeAssistant,
    mock_init_component,
) -> None:
    """Test the markdownify service can return plain text."""
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_MARKDOWNIFY,
        {
            ATTR_TEXT: "Hello, **world**!",
            ATTR_PARSER: PARSER_PLAIN_TEXT,
        },
        blocking=True,
        return_response=True,
    )

    assert response == {"messages": ["Hello, world!"]}


def test_markdownify_service_translation_keys_match_english() -> None:
    """Test all languages have the markdownify service translation keys."""
    english_paths = _leaf_paths(_load_translation("en")["services"]["markdownify"])

    for path in TRANSLATIONS_DIR.glob("*.json"):
        language = path.stem
        translation = _load_translation(language)

        assert _leaf_paths(translation["services"]["markdownify"]) == english_paths, (
            language
        )


def test_services_yaml_fields_are_translated() -> None:
    """Test services.yaml service and field keys have translations."""
    services = yaml.safe_load(SERVICES_YAML.read_text())

    for path in TRANSLATIONS_DIR.glob("*.json"):
        language = path.stem
        translation = _load_translation(language)

        assert set(translation["services"]) == set(services), language
        for service, service_config in services.items():
            assert set(translation["services"][service]["fields"]) == set(
                service_config["fields"]
            ), language
