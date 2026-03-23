"""Recursive config, options, and subentry flows."""

from collections.abc import Generator, Iterable, Mapping
from functools import partial
import inspect
from types import MappingProxyType
from typing import Any, ClassVar, cast

import voluptuous as vol

from homeassistant.config_entries import (
    HANDLERS,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigEntryState,
    ConfigError,
    ConfigFlow,
    ConfigFlowContext,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryData,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowContext,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import AbortFlow, section
from homeassistant.exceptions import HomeAssistantError

RecursiveStep = tuple[str, vol.Schema, dict[str, Any], bool]
RecursiveFlowResult = ConfigFlowResult | SubentryFlowResult

_CLASS_ATTRIBUTES_TO_COPY = (
    "VERSION",
    "MINOR_VERSION",
    "domain",
    "data_schema",
    "options_schema",
    "subentries_schema",
)
_OPTIONS_HOOKS_TO_COPY = (
    "async_validate_input",
    "step_enabled",
    "title",
    "get_data_schema",
    "get_options_schema",
    "get_default_subentries",
    "get_subentry_schema",
    "get_subentries",
)
_SUBENTRY_HOOKS_TO_COPY = (
    "async_validate_input",
    "step_enabled",
    "subentry_title",
    "get_data_schema",
    "get_options_schema",
    "get_default_subentries",
    "get_subentry_schema",
    "get_subentries",
)


class AbortRecursiveFlow(AbortFlow):
    """Error in recursive config flow."""


def _copy_recursive_hooks(
    definition_cls: type[RecursiveConfigFlow],
    adapter_base: type[RecursiveDataFlow],
    *,
    class_name: str,
    hook_names: tuple[str, ...],
    extra_attrs: dict[str, Any] | None = None,
) -> type[RecursiveDataFlow]:
    """Build an adapter class that reuses user-defined hooks."""

    namespace: dict[str, Any] = {
        "__module__": definition_cls.__module__,
        "_recursive_definition_cls": definition_cls,
    }

    for attr in _CLASS_ATTRIBUTES_TO_COPY:
        namespace[attr] = getattr(definition_cls, attr)

    for attr in hook_names:
        try:
            namespace[attr] = inspect.getattr_static(definition_cls, attr)
        except AttributeError:
            continue

    if extra_attrs is not None:
        namespace.update(extra_attrs)

    return cast(type[RecursiveDataFlow], type(class_name, (adapter_base,), namespace))


class RecursiveBaseFlow:
    """Base customization hooks for recursive flows."""

    VERSION = 1
    MINOR_VERSION = 1

    domain: ClassVar[str | None] = None
    data_schema: ClassVar[vol.Schema | None] = None
    options_schema: ClassVar[vol.Schema | None] = None
    subentries_schema: ClassVar[dict[str, vol.Schema] | None] = None
    hass: HomeAssistant

    def __init_subclass__(
        cls,
        *,
        data_schema: vol.Schema | None = None,
        options_schema: vol.Schema | None = None,
        subentries_schema: dict[str, vol.Schema] | None = None,
        **kwargs: Any,
    ) -> None:
        """Set class-level schemas if provided."""
        cls.data_schema = data_schema
        cls.options_schema = options_schema
        cls.subentries_schema = subentries_schema
        if "domain" in kwargs:
            cls.domain = kwargs["domain"]
        super().__init_subclass__(**kwargs)

    async def async_validate_input(
        self, step_id: str, user_input: dict[str, Any]
    ) -> dict[str, str]:
        """Validate step data."""
        return {}

    def step_enabled(self, step_id: str) -> bool:
        """Check if the current data flow step is enabled."""
        return True

    @property
    def title(self) -> str:
        """Return config flow title."""
        return self.domain or ""

    @property
    def subentry_title(self) -> str:
        """Return config subentry flow title."""
        if isinstance(self, ConfigSubentryFlow):
            return self.handler[1]
        raise NotImplementedError

    async def get_data_schema(self) -> vol.Schema:
        """Get data schema."""
        if self.data_schema is not None:
            return self.data_schema
        raise NotImplementedError

    async def get_options_schema(self) -> vol.Schema:
        """Get options schema."""
        if self.options_schema is not None:
            return self.options_schema
        raise NotImplementedError

    async def get_default_subentries(self) -> Iterable[ConfigSubentryData] | None:
        """Get default subentries."""
        return None

    async def get_subentry_schema(self, subentry_type: str) -> vol.Schema:
        """Get subentry schema."""
        if (
            self.subentries_schema is not None
            and subentry_type in self.subentries_schema
        ):
            return self.subentries_schema[subentry_type]
        raise NotImplementedError

    @classmethod
    @callback
    def get_subentries(cls, config_entry: ConfigEntry) -> Iterable[str]:
        """Get subentries list."""
        raise NotImplementedError

    @callback
    def _get_entry(self) -> ConfigEntry:
        """Return the config entry linked to the current flow."""
        if isinstance(self, ConfigSubentryFlow):
            return self.hass.config_entries.async_get_known_entry(self.handler[0])
        if isinstance(self, OptionsFlow):
            return self.config_entry
        if isinstance(self, ConfigFlow) and self.source == SOURCE_RECONFIGURE:
            return self._get_reconfigure_entry()
        raise ValueError("Current flow is not linked to a config entry")

    @callback
    def _get_reconfigure_subentry(self) -> ConfigSubentry:
        """Return the reconfigured subentry linked to the current context."""
        if not isinstance(self, ConfigSubentryFlow):
            raise TypeError("Current flow is not a subentry flow")
        if self.source != SOURCE_RECONFIGURE:
            raise ValueError(f"Source is {self.source}, expected {SOURCE_RECONFIGURE}")

        entry = self._get_entry()
        subentry_id = self.context["subentry_id"]
        if subentry_id not in entry.subentries:
            raise ConfigError(f"Unknown subentry {subentry_id}")
        return entry.subentries[subentry_id]

    @callback
    def _async_abort_entries_match(
        self, match_dict: dict[str, Any] | None = None
    ) -> None:
        """Abort if current entries match all data."""
        other_entries = self._get_other_entries_for_match()
        if match_dict is None:
            if other_entries:
                raise AbortFlow("already_configured")
            return

        for entry in other_entries:
            options_items = entry.options.items()
            data_items = entry.data.items()
            for item in match_dict.items():
                if item not in options_items and item not in data_items:
                    break
            else:
                raise AbortFlow("already_configured")

    @callback
    def _get_other_entries_for_match(self) -> list[ConfigEntry]:
        """Return current entries excluding the one being edited."""
        try:
            domain = self.domain or self._get_entry().domain
        except ValueError:
            if self.domain is None:
                raise
            domain = self.domain

        current_entry_id: str | None = None
        try:
            current_entry_id = self._get_entry().entry_id
        except ValueError:
            current_entry_id = None

        return [
            entry
            for entry in self.hass.config_entries.async_entries(
                domain, include_ignore=False
            )
            if current_entry_id is None or entry.entry_id != current_entry_id
        ]


class RecursiveDataFlow(RecursiveBaseFlow):
    """Shared recursive schema traversal for config-related flows."""

    _recursive_definition_cls: ClassVar[type[RecursiveBaseFlow] | None] = None

    data: dict[str, Any] | None
    options: dict[str, Any] | None
    _data_schema: vol.Schema | None
    _options_schema: vol.Schema | None

    def __init__(self) -> None:
        """Initialize the flow."""
        self.data = None
        self.options = None
        self._data_schema = None
        self._options_schema = None
        self._config_steps: Generator[RecursiveStep] | None = None
        self._current_step_id: str | None = None
        self._current_step_schema: vol.Schema | None = None
        self._current_step_data: dict[str, Any] | None = None
        self._last_step = False

    @property
    def _effective_data_schema(self) -> vol.Schema | None:
        """Return the schema used for config flow data steps."""
        if self._data_schema is not None:
            return self._data_schema
        return type(self).data_schema

    @property
    def _effective_options_schema(self) -> vol.Schema | None:
        """Return the schema used for options or subentry steps."""
        if self._options_schema is not None:
            return self._options_schema
        return type(self).options_schema

    def _config_step_generator(self) -> Generator[RecursiveStep]:
        """Return a generator of recursive step definitions."""

        def traverse_config(
            name: str,
            schema: vol.Schema,
            data: dict[str, Any],
            last_config: bool = False,
        ) -> Generator[RecursiveStep]:
            current_schema: dict[Any, Any] = {}
            recursive_schema: list[tuple[str, vol.Schema]] = []

            for var, val in schema.schema.items():
                if isinstance(val, vol.Schema):
                    recursive_schema.append((str(var), val))
                elif isinstance(val, dict):
                    recursive_schema.append((str(var), vol.Schema(val)))
                else:
                    current_schema[var] = val

            yield (
                name,
                vol.Schema(current_schema),
                data,
                last_config and not recursive_schema,
            )

            for index, (child_name, child_schema) in enumerate(
                recursive_schema, start=1
            ):
                if not self.step_enabled(child_name):
                    continue

                child_data = data.get(child_name)
                data[child_name] = (
                    child_data.copy() if isinstance(child_data, dict) else {}
                )
                yield from traverse_config(
                    child_name,
                    child_schema,
                    data[child_name],
                    last_config and index == len(recursive_schema),
                )

        if (
            not isinstance(self, (OptionsFlow, ConfigSubentryFlow))
            and self._effective_data_schema is not None
            and self.data is not None
        ):
            yield from traverse_config(
                "user",
                self._effective_data_schema,
                self.data,
                self._effective_options_schema is None,
            )

        if self._effective_options_schema is not None and self.options is not None:
            yield from traverse_config(
                "init", self._effective_options_schema, self.options, True
            )

    def _set_current_step(self, step: RecursiveStep) -> None:
        """Store the current recursive step state."""
        (
            self._current_step_id,
            self._current_step_schema,
            self._current_step_data,
            self._last_step,
        ) = step

    def _ensure_current_step(self) -> None:
        """Initialize recursive step state if needed."""
        if self._config_steps is not None:
            return

        self._config_steps = self._config_step_generator()
        self._set_current_step(next(self._config_steps))

    def _reset_recursive_state(self) -> None:
        """Reset the recursive traversal state."""
        self._config_steps = None
        self._current_step_id = None
        self._current_step_schema = None
        self._current_step_data = None
        self._last_step = False

    @staticmethod
    def _remove_missing_optional_values(
        data_schema: vol.Schema,
        step_data: dict[str, Any],
        user_input: dict[str, Any],
    ) -> None:
        """Drop optional values that were omitted from the submitted form."""
        for name in list(step_data):
            if name in user_input:
                continue
            for key in data_schema.schema:
                if key == name and isinstance(key, vol.Optional):
                    step_data.pop(name)
                    break

    async def _async_finish_recursive_flow(self) -> RecursiveFlowResult:
        """Finish the flow after the final recursive step."""
        raise NotImplementedError

    async def _async_recursive_step(
        self, step_id: str, user_input: dict[str, Any] | None = None
    ) -> RecursiveFlowResult:
        """Handle a recursive step for the current flow type."""
        try:
            self._ensure_current_step()

            if self._current_step_id != step_id:
                raise ConfigError("Unexpected step id")

            errors: dict[str, str] = {}
            if user_input is not None:
                assert self._current_step_data is not None
                assert self._current_step_schema is not None

                self._current_step_data.update(user_input)
                errors = await self.async_validate_input(
                    step_id=step_id,
                    user_input=user_input,
                )
                if not errors:
                    self._remove_missing_optional_values(
                        self._current_step_schema,
                        self._current_step_data,
                        user_input,
                    )
                    assert self._config_steps is not None
                    try:
                        self._set_current_step(next(self._config_steps))
                    except StopIteration:
                        self._reset_recursive_state()
                        return await self._async_finish_recursive_flow()

            assert self._current_step_id is not None
            assert self._current_step_schema is not None
            assert self._current_step_data is not None

        except AbortRecursiveFlow as err:
            return cast(RecursiveFlowResult, self.async_abort(reason=err.reason))

        schema = self.add_suggested_values_to_schema(
            self._current_step_schema,
            self._current_step_data,
        )

        return cast(
            RecursiveFlowResult,
            self.async_show_form(
                step_id=self._current_step_id,
                data_schema=schema,
                errors=errors,
                last_step=self._last_step,
            ),
        )

    def __getattr__(self, attr: str) -> Any:
        """Provide recursive step handlers and delegated helper methods."""
        if attr.startswith("async_step_"):
            return partial(self._async_recursive_step, attr[11:])

        definition_cls = self._recursive_definition_cls
        if definition_cls is not None and definition_cls is not type(self):
            try:
                descriptor = inspect.getattr_static(definition_cls, attr)
            except AttributeError:
                pass
            else:
                if hasattr(descriptor, "__get__"):
                    return descriptor.__get__(self, type(self))
                return descriptor

        raise AttributeError(f"{type(self).__name__!s} has no attribute {attr!r}")

    def suggested_values_from_default(
        self, data_schema: vol.Schema | Mapping[str, Any] | section
    ) -> dict[str, Any]:
        """Generate suggested values from schema markers."""
        if isinstance(data_schema, section):
            data_schema = data_schema.schema
        if isinstance(data_schema, vol.Schema):
            data_schema = data_schema.schema

        suggested_values: dict[str, Any] = {}
        for key, value in data_schema.items():  # type: ignore[union-attr]
            if hasattr(key, "default") and not isinstance(key.default, vol.Undefined):
                suggested_values[str(key)] = key.default()
            if isinstance(value, (vol.Schema, dict, section)):
                nested_values = self.suggested_values_from_default(value)
                if nested_values:
                    suggested_values[str(key)] = nested_values
        return suggested_values


class RecursiveOptionsFlow(RecursiveDataFlow, OptionsFlow):
    """Handle an options flow."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        super().__init__()
        self.data = config_entry.data.copy()
        self.options = config_entry.options.copy()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Options flow entry point."""
        try:
            if self._effective_options_schema is None:
                self._options_schema = await self.get_options_schema()
            return cast(
                ConfigFlowResult,
                await self._async_recursive_step("init", user_input),
            )
        except AbortRecursiveFlow as err:
            return self.async_abort(reason=err.reason)

    async def _async_finish_recursive_flow(self) -> ConfigFlowResult:
        """Return result entry for option flow."""
        return OptionsFlow.async_create_entry(
            self,
            title=self.title,
            data=self.options or {},
        )


class RecursiveSubentryFlow(RecursiveDataFlow, ConfigSubentryFlow):
    """Handle a config subentry flow."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a subentry."""
        return await self.async_step_init(user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle reconfiguration of a subentry."""
        self.options = self._get_reconfigure_subentry().data.copy()
        return await self.async_step_init(user_input)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Set initial options."""
        if self._get_entry().state != ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")

        try:
            if self.data is None:
                self.data = self._get_entry().data.copy()
            if self._effective_options_schema is None:
                self._options_schema = await self.get_subentry_schema(self.handler[1])
            if self.options is None:
                assert self._effective_options_schema is not None
                self.options = self.suggested_values_from_default(
                    self._effective_options_schema
                )
            return cast(
                SubentryFlowResult,
                await self._async_recursive_step("init", user_input),
            )
        except AbortRecursiveFlow as err:
            return self.async_abort(reason=err.reason)

    @property
    def title(self) -> str:
        """Return config subentry flow title."""
        return self.subentry_title

    async def _async_finish_recursive_flow(self) -> SubentryFlowResult:
        """Return result entry for subentry flow."""
        options = self.options or {}
        if self.source == "user":
            return ConfigSubentryFlow.async_create_entry(
                self,
                title=self.title,
                data=options,
            )
        return self.async_update_and_abort(
            self._get_entry(),
            self._get_reconfigure_subentry(),
            data=options,
        )


class RecursiveConfigFlow(RecursiveDataFlow, ConfigFlow):
    """Handle a config flow."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Config flow entry point."""
        try:
            if self._effective_data_schema is None:
                self._data_schema = await self.get_data_schema()
            if self._effective_options_schema is None:
                self._options_schema = await self.get_options_schema()
            if self.data is None:
                assert self._effective_data_schema is not None
                self.data = self.suggested_values_from_default(
                    self._effective_data_schema
                )
            if self.options is None:
                assert self._effective_options_schema is not None
                self.options = self.suggested_values_from_default(
                    self._effective_options_schema
                )
            return cast(
                ConfigFlowResult,
                await self._async_recursive_step("user", user_input),
            )
        except AbortRecursiveFlow as err:
            return self.async_abort(reason=err.reason)

    async def _async_finish_recursive_flow(self) -> ConfigFlowResult:
        """Return result entry for config flow."""
        return ConfigFlow.async_create_entry(
            self,
            title=self.title,
            data=self.data or {},
            options=self.options,
            subentries=await self.get_default_subentries(),
        )

    @classmethod
    @callback
    def async_get_options_flow(cls, config_entry: ConfigEntry) -> OptionsFlow:
        """Create the options flow."""
        adapter_cls = _copy_recursive_hooks(
            cls,
            RecursiveOptionsFlow,
            class_name=f"{cls.__name__}OptionsFlow",
            hook_names=_OPTIONS_HOOKS_TO_COPY,
        )
        options_adapter_cls = cast(type[RecursiveOptionsFlow], adapter_cls)
        return options_adapter_cls(config_entry)

    @classmethod
    @callback
    def async_supports_options_flow(cls, config_entry: ConfigEntry) -> bool:
        """Return options flow support for this handler."""

        def _func(obj: Any) -> Any:
            return getattr(obj, "__func__", obj)

        return bool(
            _func(cls.get_options_schema)
            is not _func(RecursiveBaseFlow.get_options_schema)
            or (cls.options_schema is not None and bool(cls.options_schema.schema))
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentries supported by this integration."""

        def _func(obj: Any) -> Any:
            return getattr(obj, "__func__", obj)

        subentry_schemas: dict[str, vol.Schema | None]
        if cls.subentries_schema is not None:
            subentry_schemas = dict(cls.subentries_schema)
        elif _func(cls.get_subentries) is not _func(RecursiveBaseFlow.get_subentries):
            subentry_schemas = dict.fromkeys(cls.get_subentries(config_entry), None)
        else:
            subentry_schemas = {}

        def subentry_factory(
            subentry_type: str,
            schema: vol.Schema | None,
        ) -> type[ConfigSubentryFlow]:
            extra_attrs: dict[str, Any] = {}
            if schema is not None:
                extra_attrs["options_schema"] = schema
            adapter_cls = _copy_recursive_hooks(
                cls,
                RecursiveSubentryFlow,
                class_name=f"{cls.__name__}{subentry_type.title()}SubentryFlow",
                hook_names=_SUBENTRY_HOOKS_TO_COPY,
                extra_attrs=extra_attrs,
            )
            return cast(type[ConfigSubentryFlow], adapter_cls)

        return {
            key: subentry_factory(key, schema)
            for key, schema in subentry_schemas.items()
        }


async def validate_data(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> MappingProxyType[str, Any]:
    """Validate config data."""
    handler = HANDLERS.get(config_entry.domain)
    if handler is None or not issubclass(handler, RecursiveConfigFlow):
        raise NotImplementedError(
            f"Handler for domain {config_entry.domain} is not a RecursiveConfigFlow"
        )
    flow = handler()
    flow.hass = hass
    flow.handler = config_entry.entry_id
    flow.context = ConfigFlowContext(
        source=SOURCE_RECONFIGURE,
        show_advanced_options=True,
        entry_id=config_entry.entry_id,
    )
    flow.data = config_entry.data.copy()
    flow.options = config_entry.options.copy()
    try:
        schema = await flow.get_data_schema()
        return MappingProxyType(schema(config_entry.data.copy()))
    except AbortRecursiveFlow as err:
        raise HomeAssistantError(
            translation_key=err.reason,
            translation_placeholders=err.description_placeholders,
        ) from err
    except vol.MultipleInvalid as err:
        raise HomeAssistantError(str(err)) from err


async def validate_options(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> MappingProxyType[str, Any]:
    """Validate options."""
    handler = HANDLERS.get(config_entry.domain)
    if handler is None or not issubclass(handler, RecursiveConfigFlow):
        raise NotImplementedError(
            f"Handler for domain {config_entry.domain} is not a RecursiveConfigFlow"
        )
    flow = cast(RecursiveOptionsFlow, handler.async_get_options_flow(config_entry))
    flow.hass = hass
    flow.handler = config_entry.entry_id
    flow.context = ConfigFlowContext(
        source=SOURCE_RECONFIGURE,
        show_advanced_options=True,
        entry_id=config_entry.entry_id,
    )
    flow.data = config_entry.data.copy()
    flow.options = config_entry.options.copy()
    try:
        schema = await flow.get_options_schema()
        return MappingProxyType(schema(config_entry.options.copy()))
    except AbortRecursiveFlow as err:
        raise HomeAssistantError(
            translation_key=err.reason,
            translation_placeholders=err.description_placeholders,
        ) from err
    except vol.MultipleInvalid as err:
        raise HomeAssistantError(str(err)) from err


async def validate_subentry_data(
    hass: HomeAssistant, config_entry: ConfigEntry, subentry_id: str
) -> MappingProxyType[str, Any]:
    """Validate subentry config."""
    handler = HANDLERS.get(config_entry.domain)
    if handler is None or not issubclass(handler, RecursiveConfigFlow):
        raise NotImplementedError(
            f"Handler for domain {config_entry.domain} is not a RecursiveConfigFlow"
        )
    subentry = config_entry.subentries[subentry_id]
    flow = cast(
        RecursiveSubentryFlow,
        handler.async_get_supported_subentry_types(config_entry)[
            subentry.subentry_type
        ](),
    )
    flow.hass = hass
    flow.handler = (config_entry.entry_id, subentry.subentry_type)
    flow.context = SubentryFlowContext(
        source=SOURCE_RECONFIGURE,
        show_advanced_options=True,
        entry_id=config_entry.entry_id,
        subentry_id=subentry.subentry_id,
    )
    flow.data = config_entry.data.copy()
    flow.options = subentry.data.copy()
    try:
        schema = await flow.get_subentry_schema(subentry.subentry_type)
        return MappingProxyType(schema(subentry.data.copy()))
    except AbortRecursiveFlow as err:
        raise HomeAssistantError(
            translation_key=err.reason,
            translation_placeholders=err.description_placeholders,
        ) from err
    except vol.MultipleInvalid as err:
        raise HomeAssistantError(str(err)) from err
