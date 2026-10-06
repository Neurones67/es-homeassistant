"""Tests du coût au tarif bleu dans un Home Assistant de test.

Le téléchargement du barème CRE est toujours simulé (aucun accès réseau).
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.es_strasbourg.const import (
    CONF_NAME,
    CONF_POWER,
    CONF_SENDER,
    COST_STATISTIC_ID,
    DEFAULT_STATISTIC_ID,
    DOMAIN,
)
from custom_components.es_strasbourg.csv_parser import parse_csv
from custom_components.es_strasbourg.tariff import TariffTable, parse_tariff_csv

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "export_consommations_energie.csv"
EMBEDDED = ROOT / "custom_components" / DOMAIN / "data" / "cre_option_base.csv"
FETCH = "custom_components.es_strasbourg.tariff_provider.async_fetch_cre_csv"


@pytest.fixture
def cre_offline():
    """CRE injoignable : le barème embarqué est utilisé."""
    with patch(FETCH, side_effect=aiohttp.ClientError("hors ligne")) as mock:
        yield mock


async def _setup(hass: HomeAssistant, tmp_path: Path, power: int | None) -> MockConfigEntry:
    await hass.config.async_set_time_zone("Europe/Paris")
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    options = {CONF_SENDER: "strasbourg", CONF_NAME: "Conso ES"}
    if power:
        options[CONF_POWER] = power
    entry = MockConfigEntry(domain=DOMAIN, title="Conso ES", data={}, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def _import_sample(hass: HomeAssistant, tmp_path: Path) -> None:
    path = tmp_path / "export.csv"
    path.write_bytes(SAMPLE.read_bytes())
    await hass.services.async_call(
        DOMAIN, "import_csv", {"path": str(path)}, blocking=True, return_response=True
    )
    await async_wait_recording_done(hass)


async def _daily_changes(hass: HomeAssistant, statistic_id: str) -> dict[date, float]:
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.start_of_local_day(date(2025, 7, 1)),
        None,
        {statistic_id},
        "day",
        None,
        {"change"},
    )
    return {
        dt_util.as_local(dt_util.utc_from_timestamp(row["start"])).date(): row["change"]
        for row in stats.get(statistic_id, [])
    }


def _expected_daily_cost(power: int, csv_text: str | None = None) -> dict[date, float]:
    """Coût attendu jour par jour : conso × prix du jour + abonnement du jour."""
    table = TariffTable(parse_tariff_csv(csv_text or EMBEDDED.read_text()))
    expected = {}
    for record in parse_csv(SAMPLE.read_bytes()):
        if record.consumption_kwh is None:
            continue
        period = table.period_for(record.day, power)
        expected[record.day] = (
            record.consumption_kwh * period.energy_ttc_per_kwh
            + period.fixed_ttc_per_day(record.day)
        )
    return expected


async def test_daily_cost_matches_tariff(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path, cre_offline
) -> None:
    await _setup(hass, tmp_path, power=6)
    await _import_sample(hass, tmp_path)

    expected = _expected_daily_cost(6)
    changes = await _daily_changes(hass, COST_STATISTIC_ID)
    assert set(changes) >= set(expected)
    for day, cost in expected.items():
        assert changes[day] == pytest.approx(cost, abs=1e-3), day
    # Passage au tarif du 01/02/2026 : le prix du kWh change ce jour-là.
    jan31, feb1 = date(2026, 1, 31), date(2026, 2, 1)
    assert expected[jan31] / _kwh(jan31) != pytest.approx(expected[feb1] / _kwh(feb1))


def _kwh(day: date) -> int:
    return next(r.consumption_kwh for r in parse_csv(SAMPLE.read_bytes()) if r.day == day)


async def test_no_cost_without_power(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path, cre_offline
) -> None:
    await _setup(hass, tmp_path, power=None)
    await _import_sample(hass, tmp_path)
    assert await _daily_changes(hass, COST_STATISTIC_ID) == {}
    assert await _daily_changes(hass, DEFAULT_STATISTIC_ID)
    cre_offline.assert_not_called()  # pas de téléchargement inutile


async def test_power_change_recomputes_history(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path, cre_offline
) -> None:
    entry = await _setup(hass, tmp_path, power=6)
    await _import_sample(hass, tmp_path)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SENDER: "strasbourg", CONF_NAME: "Conso ES", CONF_POWER: "9"}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    changes = await _daily_changes(hass, COST_STATISTIC_ID)
    for day, cost in _expected_daily_cost(9).items():
        assert changes[day] == pytest.approx(cost, abs=1e-3), day


async def test_tariff_refresh_from_cre_recomputes_cost(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path, cre_offline
) -> None:
    await _setup(hass, tmp_path, power=6)
    await _import_sample(hass, tmp_path)

    # Nouveau barème publié par la CRE : kWh du 01/08/2025 corrigé à 0,30 €.
    revised = EMBEDDED.read_text().replace(
        "01/08/2025;31/01/2026;6;132,84;185,94;0,1327;0,1952",
        "01/08/2025;31/01/2026;6;132,84;185,94;0,1327;0,3000",
    )
    assert revised != EMBEDDED.read_text()
    cre_offline.side_effect = None
    cre_offline.return_value = revised
    calls_at_startup = cre_offline.call_count

    # Contrôle hebdomadaire : rien au bout de 2 jours…
    now = dt_util.utcnow()
    async_fire_time_changed(hass, now + timedelta(days=2))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert cre_offline.call_count == calls_at_startup

    # … le nouveau barème est récupéré au bout d'une semaine.
    async_fire_time_changed(hass, now + timedelta(days=7, minutes=1))
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    assert cre_offline.call_count == calls_at_startup + 1

    changes = await _daily_changes(hass, COST_STATISTIC_ID)
    sep1 = date(2025, 9, 1)
    assert changes[sep1] == pytest.approx(_expected_daily_cost(6, revised)[sep1], abs=1e-3)
    assert changes[sep1] != pytest.approx(_expected_daily_cost(6)[sep1], abs=1e-3)


async def test_downloaded_tariff_cached_for_next_start(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    revised = EMBEDDED.read_text().replace("0,2001", "0,2500")
    with patch(FETCH, AsyncMock(return_value=revised)):
        entry = await _setup(hass, tmp_path, power=6)
    assert "CRE" in entry.runtime_data.tariffs.source

    # Redémarrage hors ligne : le barème téléchargé est rechargé depuis .storage.
    with patch(FETCH, side_effect=aiohttp.ClientError):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    table = entry.runtime_data.tariffs.table
    assert table.period_for(date(2026, 9, 1), 6).energy_ttc_per_kwh == 0.25


async def test_same_tariff_with_crlf_is_not_a_change(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    """La CRE sert le fichier en CRLF ; identique à la copie embarquée → rien à refaire."""
    crlf = EMBEDDED.read_bytes().decode()
    assert "\r\n" in crlf
    with patch(FETCH, AsyncMock(return_value=crlf)):
        entry = await _setup(hass, tmp_path, power=6)
    assert entry.runtime_data.tariffs.source == "copie embarquée"


async def test_invalid_download_keeps_tariff(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    with patch(FETCH, AsyncMock(return_value="<html>Maintenance</html>")):
        entry = await _setup(hass, tmp_path, power=6)
    assert entry.runtime_data.tariffs.source == "copie embarquée"
    assert entry.runtime_data.tariffs.table.period_for(date(2026, 9, 1), 6) is not None


async def test_clear_statistics_also_clears_cost(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path, cre_offline
) -> None:
    await _setup(hass, tmp_path, power=6)
    await _import_sample(hass, tmp_path)
    assert await _daily_changes(hass, COST_STATISTIC_ID)

    await hass.services.async_call(DOMAIN, "clear_statistics", {}, blocking=True)
    await async_wait_recording_done(hass)
    assert await _daily_changes(hass, COST_STATISTIC_ID) == {}
    assert await _daily_changes(hass, DEFAULT_STATISTIC_ID) == {}
