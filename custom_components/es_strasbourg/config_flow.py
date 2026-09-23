"""Assistant de configuration (interface) de l'intégration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    ConfigEntrySelector,
    ConfigEntrySelectorConfig,
    TextSelector,
)

from .const import (
    CONF_IMAP_ENTRY,
    CONF_NAME,
    CONF_SENDER,
    DEFAULT_NAME,
    DEFAULT_SENDER,
    DOMAIN,
    IMAP_DOMAIN,
)


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    """Formulaire commun à l'assistant et à l'écran Options."""
    imap_entry = defaults.get(CONF_IMAP_ENTRY)
    return vol.Schema(
        {
            # Optionnel : sans compte choisi, tous les comptes IMAP sont écoutés.
            vol.Optional(
                CONF_IMAP_ENTRY,
                description={"suggested_value": imap_entry} if imap_entry else None,
            ): ConfigEntrySelector(ConfigEntrySelectorConfig(integration=IMAP_DOMAIN)),
            vol.Optional(
                CONF_SENDER, default=defaults.get(CONF_SENDER, DEFAULT_SENDER)
            ): TextSelector(),
            vol.Required(
                CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME)
            ): TextSelector(),
        }
    )


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    """Normalise la saisie : expéditeur en minuscules, champs vides retirés."""
    options = dict(user_input)
    options[CONF_SENDER] = (options.get(CONF_SENDER) or "").strip().lower()
    options[CONF_NAME] = options[CONF_NAME].strip() or DEFAULT_NAME
    if not options.get(CONF_IMAP_ENTRY):
        options.pop(CONF_IMAP_ENTRY, None)
    return options


class EsStrasbourgConfigFlow(ConfigFlow, domain=DOMAIN):
    """Assistant : une seule instance (single_config_entry dans le manifest)."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            options = _clean(user_input)
            return self.async_create_entry(
                title=options[CONF_NAME], data={}, options=options
            )

        return self.async_show_form(step_id="user", data_schema=_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EsStrasbourgOptionsFlow:
        return EsStrasbourgOptionsFlow()


class EsStrasbourgOptionsFlow(OptionsFlowWithReload):
    """Modification des réglages ; l'entrée est rechargée automatiquement."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=_clean(user_input))

        return self.async_show_form(
            step_id="init", data_schema=_schema(dict(self.config_entry.options))
        )
