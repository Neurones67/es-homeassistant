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
"""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN, UNIT_KWH
from .csv_parser import ConsumptionRecord

_LOGGER = logging.getLogger(__name__)


def _build_metadata(statistic_id: str, name: str) -> StatisticMetaData:
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
        "unit_of_measurement": UNIT_KWH,
        "has_mean": False,
        "has_sum": True,
    }

    try:
        from homeassistant.components.recorder.models import StatisticMeanType

        metadata["mean_type"] = StatisticMeanType.NONE  # type: ignore[typeddict-unknown-key]
    except ImportError:  # cœur antérieur à l'introduction de mean_type
        pass

    # kWh appartient au convertisseur d'énergie.
    metadata["unit_class"] = "energy"  # type: ignore[typeddict-unknown-key]
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
        start = dt_util.as_utc(dt_util.start_of_local_day(record.day)) - timedelta(hours=1)
        value = float(record.index_kwh)
        statistics.append(StatisticData(start=start, state=value, sum=value + offset))

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
