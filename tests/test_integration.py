"""Tests de l'intégration dans un Home Assistant de test.

Utilise pytest-homeassistant-custom-component : un vrai ``hass`` avec un recorder
SQLite en mémoire. On relit les statistiques comme le tableau Énergie (``change``
par jour) et on les compare aux consommations du CSV.

    .venv/bin/python -m pytest -q
"""

from __future__ import annotations

import base64
from datetime import date, timedelta
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.es_strasbourg.const import (
    CONF_IMAP_ENTRY,
    CONF_NAME,
    CONF_SENDER,
    DEFAULT_STATISTIC_ID,
    DOMAIN,
)
from custom_components.es_strasbourg.csv_parser import parse_csv

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "export_consommations_energie.csv"
ES_SENDER = "vos-mesures-maconsolinky@strasbourg-electricite-reseaux.fr"


# Ordre des fixtures important : recorder_mock doit précéder hass, puis
# enable_custom_integrations autorise le chargement de custom_components/.
@pytest.fixture
async def es_hass(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path: Path
) -> HomeAssistant:
    """hass en heure de Paris, avec es_strasbourg configuré et tmp_path autorisé."""
    await hass.config.async_set_time_zone("Europe/Paris")
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    await _setup_entry(hass, {CONF_SENDER: "strasbourg", CONF_NAME: "Conso ES"})
    return hass


async def _setup_entry(hass: HomeAssistant, options: dict) -> MockConfigEntry:
    """Ajoute l'intégration comme le ferait l'assistant de configuration."""
    entry = MockConfigEntry(domain=DOMAIN, title="Conso ES", data={}, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _expected_daily() -> dict[date, int]:
    """Consommation journalière attendue, telle qu'écrite dans le CSV."""
    return {
        r.day: r.consumption_kwh
        for r in parse_csv(SAMPLE.read_bytes())
        if r.consumption_kwh is not None
    }


async def _daily_changes(hass: HomeAssistant, first: date, last: date) -> dict[date, float]:
    """Consommation par jour local, calculée par HA comme dans le tableau Énergie."""
    start = dt_util.start_of_local_day(first)
    end = dt_util.start_of_local_day(last + timedelta(days=1))
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {DEFAULT_STATISTIC_ID},
        "day",
        None,
        {"change"},
    )
    return {
        dt_util.as_local(dt_util.utc_from_timestamp(row["start"])).date(): row["change"]
        for row in stats.get(DEFAULT_STATISTIC_ID, [])
    }


async def _import_sample(hass: HomeAssistant, tmp_path: Path) -> dict:
    path = tmp_path / "export.csv"
    path.write_bytes(SAMPLE.read_bytes())
    response = await hass.services.async_call(
        DOMAIN, "import_csv", {"path": str(path)}, blocking=True, return_response=True
    )
    await async_wait_recording_done(hass)
    return response


async def test_import_csv_daily_consumption_matches(es_hass: HomeAssistant, tmp_path: Path) -> None:
    """Chaque jour du tableau Énergie doit afficher la consommation du CSV."""
    response = await _import_sample(es_hass, tmp_path)
    assert response["imported"] == 351

    expected = _expected_daily()
    changes = await _daily_changes(es_hass, min(expected), max(expected))
    mismatches = {
        day: (changes.get(day), kwh)
        for day, kwh in expected.items()
        if changes.get(day) != kwh
    }
    assert not mismatches, (
        f"{len(mismatches)} jours faux (HA, CSV), ex. : "
        f"{dict(list(sorted(mismatches.items()))[:5])}"
    )


async def test_no_spike_before_first_day(es_hass: HomeAssistant, tmp_path: Path) -> None:
    """La série démarre à 0 : pas de pic de 10 000 kWh au premier jour."""
    await _import_sample(es_hass, tmp_path)
    expected = _expected_daily()
    first = min(expected)
    changes = await _daily_changes(es_hass, first - timedelta(days=3), first)
    assert changes.get(first - timedelta(days=1), 0) == 0
    assert changes[first] == expected[first]


async def test_incremental_imports_like_weekly_mails(
    es_hass: HomeAssistant, tmp_path: Path
) -> None:
    """Deux exports qui se chevauchent (mails successifs) = un seul export complet."""
    lines = SAMPLE.read_bytes().split(b"\n")
    header, rows = lines[:2], lines[2:]
    for i, chunk in enumerate((rows[:200], rows[150:])):
        path = tmp_path / f"part{i}.csv"
        path.write_bytes(b"\n".join(header + chunk))
        await es_hass.services.async_call(
            DOMAIN, "import_csv", {"path": str(path)}, blocking=True, return_response=True
        )
        await async_wait_recording_done(es_hass)

    expected = _expected_daily()
    assert await _daily_changes(es_hass, min(expected), max(expected)) == expected


async def test_import_is_idempotent(es_hass: HomeAssistant, tmp_path: Path) -> None:
    """Réimporter le même CSV ne double pas la consommation."""
    await _import_sample(es_hass, tmp_path)
    expected = _expected_daily()
    first = await _daily_changes(es_hass, min(expected), max(expected))
    await _import_sample(es_hass, tmp_path)
    assert await _daily_changes(es_hass, min(expected), max(expected)) == first


async def test_import_csv_rejects_forbidden_path(es_hass: HomeAssistant) -> None:
    with pytest.raises(Exception, match="non autorisé"):
        await es_hass.services.async_call(
            DOMAIN, "import_csv", {"path": "/etc/passwd"}, blocking=True, return_response=True
        )


async def test_import_csv_config_dir_not_allowed_by_default(es_hass: HomeAssistant) -> None:
    """Le dossier de config n'est pas autorisé d'office : message explicite."""
    with pytest.raises(ServiceValidationError, match="allowlist_external_dirs"):
        await es_hass.services.async_call(
            DOMAIN,
            "import_csv",
            {"path": es_hass.config.path("export.csv")},
            blocking=True,
            return_response=True,
        )


def _register_fake_fetch_part(hass: HomeAssistant) -> list[dict]:
    """Remplace imap.fetch_part par un faux renvoyant le CSV en base64."""
    calls: list[dict] = []

    async def _fetch_part(call: ServiceCall) -> dict:
        calls.append(dict(call.data))
        return {
            "part_data": base64.b64encode(SAMPLE.read_bytes()).decode(),
            # Valeurs brutes, comme les renvoie HA pour le vrai mail ES.
            "content_type": "application/pdf",
            "content_transfer_encoding": "BASE64",
            "filename": "=?UTF-8?Q?Consommation=5fJournaliere.csv?=",
            "part": call.data["part"],
            "uid": call.data["uid"],
        }

    hass.services.async_register(
        "imap", "fetch_part", _fetch_part, supports_response=SupportsResponse.ONLY
    )
    return calls


def _imap_event(sender: str) -> dict:
    """Événement imap_content tel que reçu d'un vrai mail ES (HA 2026.9).

    Pièges réels : le CSV est déclaré en application/pdf et son nom est
    encodé MIME (RFC 2047).
    """
    return {
        "entry_id": "entry-test",
        "uid": "42",
        "sender": sender,
        "subject": "Vos données de consommation",
        "parts": {
            "0": {"content_type": "text/html", "content_transfer_encoding": "QUOTED-PRINTABLE"},
            "1": {
                "content_type": "application/pdf",
                "filename": "=?UTF-8?Q?Consommation=5fJournaliere.csv?=",
                "content_transfer_encoding": "BASE64",
            },
        },
    }


async def test_imap_event_imports_attachment(es_hass: HomeAssistant) -> None:
    calls = _register_fake_fetch_part(es_hass)

    es_hass.bus.async_fire("imap_content", _imap_event(ES_SENDER))
    await es_hass.async_block_till_done()
    await async_wait_recording_done(es_hass)

    assert calls == [{"entry": "entry-test", "uid": "42", "part": "1"}]
    expected = _expected_daily()
    changes = await _daily_changes(es_hass, min(expected), max(expected))
    assert len(changes) >= len(expected) - 1


async def test_imap_event_other_sender_ignored(es_hass: HomeAssistant) -> None:
    calls = _register_fake_fetch_part(es_hass)

    es_hass.bus.async_fire("imap_content", _imap_event("newsletter@example.com"))
    await es_hass.async_block_till_done()

    assert calls == []


async def test_imap_event_other_account_ignored(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Avec un compte IMAP choisi, les mails des autres comptes sont ignorés."""
    await _setup_entry(hass, {CONF_IMAP_ENTRY: "autre-compte", CONF_SENDER: "strasbourg"})
    calls = _register_fake_fetch_part(hass)

    hass.bus.async_fire("imap_content", _imap_event(ES_SENDER))  # entry_id "entry-test"
    await hass.async_block_till_done()

    assert calls == []


async def test_import_csv_without_config_entry(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    """L'action existe dès le chargement, mais exige l'intégration configurée."""
    assert await async_setup_component(hass, DOMAIN, {})
    with pytest.raises(ServiceValidationError, match="non configurée"):
        await hass.services.async_call(
            DOMAIN, "import_csv", {}, blocking=True, return_response=True
        )


async def test_unload_stops_listening(es_hass: HomeAssistant) -> None:
    calls = _register_fake_fetch_part(es_hass)
    entry = es_hass.config_entries.async_entries(DOMAIN)[0]
    assert await es_hass.config_entries.async_unload(entry.entry_id)

    es_hass.bus.async_fire("imap_content", _imap_event(ES_SENDER))
    await es_hass.async_block_till_done()

    assert calls == []
