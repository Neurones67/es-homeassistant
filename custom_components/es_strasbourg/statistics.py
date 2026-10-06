"""Injection des relevés ES dans les statistiques long terme de Home Assistant.

On alimente une statistique externe « cumulée » (``has_sum=True``). Home Assistant
calcule la consommation d'une période comme la différence des ``sum`` à ses bornes.

Horodatage : une ligne ``start = T`` couvre l'heure ``[T, T+1h)`` et son ``sum``
est le total **à la fin** de cette heure. L'Index du jour J étant le relevé au
début de J (``Conso(J) = Index(J+1) - Index(J)``), on le pose sur l'heure qui se
termine à minuit, soit J-1 23:00 locale. La conso de J tombe ainsi dans le jour J.

Valeurs : ``state`` = Index brut, ``sum`` = Index - décalage, avec un décalage
choisi pour que la série démarre à 0 (sinon HA compterait tout l'Index comme
consommation du premier jour). Le décalage est relu dans la dernière statistique
existante (``sum - state``) pour rester cohérent d'un import à l'autre.

Réinjecter un jour déjà présent écrase la valeur (même horodatage) : le mécanisme
est donc idempotent, et corrige naturellement une valeur « estimée » remplacée
plus tard par une « réelle ».

Coût : une seconde statistique (``es_strasbourg:cost``, en euros) suit les mêmes
horodatages. Elle n'a pas d'ancre absolue comme l'Index : on la **recalcule
entièrement** depuis la statistique d'énergie après chaque import ou changement
de tarif, ce qui la garde cohérente quels que soient l'ordre et le chevauchement
des imports.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN, UNIT_EUR, UNIT_KWH
from .csv_parser import ConsumptionRecord
from .tariff import TariffTable, compute_cumulative_cost

_LOGGER = logging.getLogger(__name__)


def _build_metadata(
    statistic_id: str, name: str, unit: str = UNIT_KWH, unit_class: str | None = "energy"
) -> StatisticMetaData:
    """Construit les métadonnées, en restant compatible avec l'API récente.

    Depuis HA 2025.11+/2026.11 : ``mean_type`` et ``unit_class`` remplacent/complètent
    ``has_mean``. On les ajoute quand ils sont disponibles pour éviter les warnings
    de dépréciation, tout en gardant ``has_mean`` pour les cœurs plus anciens.
    """
    source = statistic_id.split(":", 1)[0]
    metadata: StatisticMetaData = {
        "source": source,
        "statistic_id": statistic_id,
        "name": name,
        "unit_of_measurement": unit,
        "has_mean": False,
        "has_sum": True,
    }

    try:
        from homeassistant.components.recorder.models import StatisticMeanType

        metadata["mean_type"] = StatisticMeanType.NONE  # type: ignore[typeddict-unknown-key]
    except ImportError:  # cœur antérieur à l'introduction de mean_type
        pass

    # kWh appartient au convertisseur d'énergie ; l'euro n'a pas de convertisseur.
    metadata["unit_class"] = unit_class  # type: ignore[typeddict-unknown-key]
    return metadata


async def _async_get_offset(hass: HomeAssistant, statistic_id: str) -> float | None:
    """Décalage ``sum - state`` de la dernière statistique existante, sinon None."""
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, False, {"state", "sum"}
    )
    rows = last.get(statistic_id)
    if not rows or rows[0].get("state") is None or rows[0].get("sum") is None:
        return None
    return rows[0]["sum"] - rows[0]["state"]


async def async_import_records(
    hass: HomeAssistant,
    records: list[ConsumptionRecord],
    statistic_id: str,
    name: str,
) -> int:
    """Injecte les relevés dans les statistiques externes. Renvoie le nb de points."""
    # Sans index, impossible d'alimenter une statistique cumulée.
    indexed = [r for r in records if r.index_kwh is not None]
    if not indexed:
        _LOGGER.warning("Aucun relevé exploitable à injecter (Index manquant)")
        return 0

    offset = await _async_get_offset(hass, statistic_id)
    if offset is None:
        # Première injection : la série démarre à 0.
        offset = -float(indexed[0].index_kwh)

    statistics: list[StatisticData] = []
    for record in indexed:
        value = float(record.index_kwh)
        statistics.append(
            StatisticData(start=_start_for_day(record.day), state=value, sum=value + offset)
        )

    metadata = _build_metadata(statistic_id, name)
    async_add_external_statistics(hass, metadata, statistics)
    _LOGGER.info(
        "%s : %d points de statistique injectés (%s → %s)",
        DOMAIN,
        len(statistics),
        indexed[0].day,
        indexed[-1].day,
    )
    return len(statistics)


def _start_for_day(day) -> datetime:
    """Horodatage de la ligne portant l'Index du jour J : J-1 23:00 locale."""
    return dt_util.as_utc(dt_util.start_of_local_day(day)) - timedelta(hours=1)


async def async_rebuild_cost(
    hass: HomeAssistant,
    energy_statistic_id: str,
    cost_statistic_id: str,
    name: str,
    table: TariffTable,
    power_kva: int,
) -> int:
    """Recalcule toute la statistique de coût depuis l'énergie. Renvoie le nb de points."""
    recorder = get_instance(hass)
    # Les imports d'énergie sont mis en file : on attend qu'ils soient écrits.
    await recorder.async_block_till_done()
    rows = (
        await recorder.async_add_executor_job(
            statistics_during_period,
            hass,
            datetime(2000, 1, 1, tzinfo=UTC),
            None,
            {energy_statistic_id},
            "hour",
            None,
            {"state"},
        )
    ).get(energy_statistic_id, [])

    points = [
        # La ligne J-1 23:00 porte l'Index du début du jour J.
        (dt_util.as_local(dt_util.utc_from_timestamp(row["start"]) + timedelta(hours=1)).date(),
         row["state"])
        for row in rows
        if row.get("state") is not None
    ]
    cumulative, missing = compute_cumulative_cost(points, table, power_kva)
    if missing:
        _LOGGER.warning(
            "Pas de tarif bleu %d kVA pour %d jour(s) (%s → %s) : coût compté à 0 €",
            power_kva,
            len(missing),
            missing[0],
            missing[-1],
        )
    if not cumulative:
        return 0

    statistics = [
        StatisticData(start=_start_for_day(day), state=cost, sum=cost)
        for day, cost in cumulative
    ]
    metadata = _build_metadata(cost_statistic_id, name, UNIT_EUR, None)
    async_add_external_statistics(hass, metadata, statistics)
    _LOGGER.info(
        "%s : coût recalculé sur %d points (%.2f € au total, %d kVA)",
        DOMAIN,
        len(statistics),
        cumulative[-1][1],
        power_kva,
    )
    return len(statistics)
