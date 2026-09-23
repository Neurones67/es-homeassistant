"""Tests de l'assistant de configuration et de l'écran Options."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.es_strasbourg.const import (
    CONF_IMAP_ENTRY,
    CONF_NAME,
    CONF_SENDER,
    DEFAULT_NAME,
    DEFAULT_SENDER,
    DOMAIN,
)

INTEGRATION_DIR = (
    Path(__file__).resolve().parent.parent / "custom_components" / DOMAIN
)


@pytest.fixture
async def flow_hass(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> HomeAssistant:
    return hass


async def test_user_flow_creates_entry(flow_hass: HomeAssistant) -> None:
    result = await flow_hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    # Valeurs par défaut proposées dans le formulaire.
    defaults = {
        str(key): key.default() for key in result["data_schema"].schema if callable(key.default)
    }
    assert defaults == {CONF_SENDER: DEFAULT_SENDER, CONF_NAME: DEFAULT_NAME}

    result = await flow_hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SENDER: "  Strasbourg ", CONF_NAME: "Ma conso"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Ma conso"
    # Expéditeur normalisé, pas de compte IMAP → clé absente.
    assert result["options"] == {CONF_SENDER: "strasbourg", CONF_NAME: "Ma conso"}
    await flow_hass.async_block_till_done()
    assert flow_hass.config_entries.async_loaded_entries(DOMAIN)


async def test_single_instance(flow_hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, options={CONF_NAME: DEFAULT_NAME}).add_to_hass(flow_hass)

    result = await flow_hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_options_flow_updates_and_reloads(flow_hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, options={CONF_SENDER: "strasbourg", CONF_NAME: DEFAULT_NAME}
    )
    entry.add_to_hass(flow_hass)
    assert await flow_hass.config_entries.async_setup(entry.entry_id)
    await flow_hass.async_block_till_done()

    result = await flow_hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await flow_hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_IMAP_ENTRY: "compte-imap", CONF_SENDER: "", CONF_NAME: "Autre nom"},
    )
    await flow_hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_IMAP_ENTRY: "compte-imap",
        CONF_SENDER: "",
        CONF_NAME: "Autre nom",
    }
    assert flow_hass.config_entries.async_loaded_entries(DOMAIN) == [entry]


@pytest.mark.parametrize("lang", ["fr", "en"])
def test_translations_cover_form_fields(lang: str) -> None:
    """Chaque champ du formulaire a un libellé, dans l'assistant et les options."""
    strings = json.loads((INTEGRATION_DIR / "translations" / f"{lang}.json").read_text())
    fields = {CONF_IMAP_ENTRY, CONF_SENDER, CONF_NAME}
    assert set(strings["config"]["step"]["user"]["data"]) == fields
    assert set(strings["options"]["step"]["init"]["data"]) == fields
    assert "import_csv" in strings["services"]
