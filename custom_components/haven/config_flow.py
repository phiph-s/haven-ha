"""Config Flow: eine Instanz, Option Z2M-Basis-Topic."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .const import CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC, DOMAIN


def _clean_topic(value: str) -> str | None:
    """Gibt das bereinigte Topic zurück oder None, wenn es ungültig ist."""
    topic = value.strip().strip("/")
    if not topic or any(ch in topic for ch in "#+"):
        return None
    return topic


class HavenConfigFlow(ConfigFlow, domain=DOMAIN):
    """Einrichtung über die Oberfläche."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # Zusätzlich zu "single_config_entry" im Manifest.
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        errors: dict[str, str] = {}
        if user_input is not None:
            topic = _clean_topic(user_input[CONF_BASE_TOPIC])
            if topic is None:
                errors[CONF_BASE_TOPIC] = "invalid_topic"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Haven", data={CONF_BASE_TOPIC: topic})

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_BASE_TOPIC, default=DEFAULT_BASE_TOPIC): str}
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HavenOptionsFlow:
        return HavenOptionsFlow()


class HavenOptionsFlow(OptionsFlow):
    """Optionen: Basis-Topic ändern (self.config_entry stellt HA bereit)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = self.config_entry.options.get(
            CONF_BASE_TOPIC, self.config_entry.data.get(CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC)
        )
        if user_input is not None:
            topic = _clean_topic(user_input[CONF_BASE_TOPIC])
            if topic is None:
                errors[CONF_BASE_TOPIC] = "invalid_topic"
            else:
                return self.async_create_entry(data={CONF_BASE_TOPIC: topic})

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required(CONF_BASE_TOPIC, default=current): str}),
            errors=errors,
        )
