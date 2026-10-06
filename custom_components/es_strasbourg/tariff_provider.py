"""Fourniture du barème tarif bleu : CRE en ligne, cache local, copie embarquée.

Ordre de priorité au chargement : dernier CSV téléchargé (``.storage``), sinon la
copie embarquée dans ``data/``. Le CSV de la CRE est ensuite retéléchargé
périodiquement ; un fichier invalide ou injoignable laisse le barème en place.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import CRE_TARIFF_URL, DOMAIN, EMBEDDED_TARIFF_FILE
from .tariff import TariffPeriod, TariffTable, parse_tariff_csv

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
FETCH_TIMEOUT = 30


async def async_fetch_cre_csv(hass: HomeAssistant) -> str:
    """Télécharge le CSV « Option Base » de la CRE (texte ASCII)."""
    session = async_get_clientsession(hass)
    async with asyncio.timeout(FETCH_TIMEOUT):
        response = await session.get(CRE_TARIFF_URL)
        response.raise_for_status()
        return (await response.read()).decode("utf-8-sig", errors="replace")


class TariffProvider:
    """Détient le barème courant et sait le rafraîchir depuis la CRE."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.tariffs")
        self._periods: list[TariffPeriod] | None = None
        self.table: TariffTable | None = None
        self.source: str = "aucune"

    async def async_load(self) -> None:
        """Charge le cache local, ou à défaut la copie embarquée."""
        stored = await self._store.async_load()
        if stored and self._apply(stored.get("csv", "")):
            self.source = f"CRE (téléchargé le {stored.get('fetched', '?')})"
            return

        path = Path(__file__).parent / EMBEDDED_TARIFF_FILE
        embedded = await self._hass.async_add_executor_job(path.read_text, "utf-8")
        if not self._apply(embedded):
            raise ValueError(f"Barème embarqué illisible : {path}")
        self.source = "copie embarquée"

    async def async_refresh(self) -> bool:
        """Retélécharge le barème CRE. Renvoie True s'il a changé."""
        try:
            csv_text = await async_fetch_cre_csv(self._hass)
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            _LOGGER.warning("Barème CRE injoignable (%s) : on garde %s", err, self.source)
            return False

        try:
            periods = parse_tariff_csv(csv_text)
        except ValueError as err:
            _LOGGER.warning("Barème CRE téléchargé illisible (%s) : on garde %s", err, self.source)
            return False
        # Comparaison des barèmes, pas du texte (fins de ligne CRLF/LF).
        if periods == self._periods:
            return False
        self._apply(csv_text)

        fetched = dt_util.now().date().isoformat()
        await self._store.async_save({"csv": csv_text, "fetched": fetched})
        self.source = f"CRE (téléchargé le {fetched})"
        _LOGGER.info("Barème tarif bleu mis à jour depuis la CRE")
        return True

    def _apply(self, csv_text: str) -> bool:
        try:
            periods = parse_tariff_csv(csv_text)
        except ValueError as err:
            _LOGGER.debug("CSV tarifaire rejeté : %s", err)
            return False
        self._periods, self.table = periods, TariffTable(periods)
        return True
