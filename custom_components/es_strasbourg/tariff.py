"""Tarif bleu réglementé (option Base) et calcul du coût de la consommation.

Module sans dépendance à Home Assistant, testable isolément.

Source : historique open data de la CRE, ``Option_Base.csv`` ::

    DATE_DEBUT;DATE_FIN;P_SOUSCRITE;PART_FIXE_HT;PART_FIXE_TTC;PART_VARIABLE_HT;PART_VARIABLE_TTC
    01/08/2026;;6;143,28;190,81;0,1361;0,2001

* ``DATE_FIN`` vide : tarif en vigueur ;
* ``PART_FIXE_*`` : abonnement annuel en euros ; ``PART_VARIABLE_*`` : €/kWh.

Le barème est national (France métropolitaine continentale) : le prix du kWh
appliqué par ÉS est identique ; l'abonnement TTC peut différer de quelques
dizaines de centimes par an (taxes propres au réseau de distribution).
"""

from __future__ import annotations

import calendar
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import io

__all__ = ["TariffPeriod", "TariffTable", "compute_cumulative_cost", "parse_tariff_csv"]

_REQUIRED_COLUMNS = {
    "DATE_DEBUT",
    "DATE_FIN",
    "P_SOUSCRITE",
    "PART_FIXE_TTC",
    "PART_VARIABLE_TTC",
}


@dataclass(frozen=True)
class TariffPeriod:
    """Barème TTC d'une puissance souscrite sur une période de validité."""

    start: date
    end: date | None  # inclus ; None = tarif en vigueur
    power_kva: int
    fixed_ttc_per_year: float  # abonnement annuel, €
    energy_ttc_per_kwh: float  # €/kWh

    def covers(self, day: date) -> bool:
        return self.start <= day and (self.end is None or day <= self.end)

    def fixed_ttc_per_day(self, day: date) -> float:
        """Abonnement réparti au prorata des jours de l'année."""
        return self.fixed_ttc_per_year / (366 if calendar.isleap(day.year) else 365)


def _parse_date(raw: str) -> date | None:
    raw = raw.strip()
    return datetime.strptime(raw, "%d/%m/%Y").date() if raw else None


def _parse_decimal(raw: str) -> float:
    return float(raw.strip().replace(" ", "").replace(",", "."))


def parse_tariff_csv(content: bytes | str) -> list[TariffPeriod]:
    """Parse le CSV « Option Base » de la CRE.

    Raises:
        ValueError: format inattendu (colonnes manquantes, aucune ligne valide).
    """
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(content), delimiter=";")
    missing = _REQUIRED_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"Colonnes absentes du CSV tarifaire : {sorted(missing)}")

    periods: list[TariffPeriod] = []
    for row in reader:
        try:
            start = _parse_date(row["DATE_DEBUT"])
            if start is None:
                continue
            periods.append(
                TariffPeriod(
                    start=start,
                    end=_parse_date(row["DATE_FIN"]),
                    power_kva=int(row["P_SOUSCRITE"].strip()),
                    fixed_ttc_per_year=_parse_decimal(row["PART_FIXE_TTC"]),
                    energy_ttc_per_kwh=_parse_decimal(row["PART_VARIABLE_TTC"]),
                )
            )
        except (ValueError, AttributeError):
            continue  # ligne incomplète : ignorée
    if not periods:
        raise ValueError("Aucun tarif exploitable dans le CSV tarifaire")
    return periods


class TariffTable:
    """Recherche du barème applicable à un jour et une puissance donnés."""

    def __init__(self, periods: list[TariffPeriod]) -> None:
        self._by_power: dict[int, list[TariffPeriod]] = {}
        for period in sorted(periods, key=lambda p: p.start):
            self._by_power.setdefault(period.power_kva, []).append(period)

    @property
    def powers(self) -> list[int]:
        return sorted(self._by_power)

    def period_for(self, day: date, power_kva: int) -> TariffPeriod | None:
        for period in reversed(self._by_power.get(power_kva, [])):
            if period.covers(day):
                return period
        return None


def compute_cumulative_cost(
    points: list[tuple[date, float]], table: TariffTable, power_kva: int
) -> tuple[list[tuple[date, float]], list[date]]:
    """Coût TTC cumulé (kWh + abonnement) à chaque relevé d'Index.

    Args:
        points: ``(jour J, Index au début de J)`` triés par jour, comme les
            statistiques d'énergie.
        table: barèmes du tarif bleu.
        power_kva: puissance souscrite.

    Returns:
        ``(jour J, coût cumulé au début de J)`` — 0 au premier relevé — et la
        liste des jours sans tarif connu (comptés à 0 €).

    Entre deux relevés espacés de plusieurs jours (``NC``), l'énergie est
    répartie également sur les jours de l'intervalle, chacun à son tarif.
    """
    if not points:
        return [], []

    total = 0.0
    result = [(points[0][0], 0.0)]
    missing: list[date] = []
    for (day_a, index_a), (day_b, index_b) in zip(points, points[1:]):
        days = (day_b - day_a).days
        if days <= 0:
            continue
        energy_per_day = (index_b - index_a) / days
        for offset in range(days):
            day = day_a + timedelta(days=offset)
            period = table.period_for(day, power_kva)
            if period is None:
                missing.append(day)
                continue
            total += energy_per_day * period.energy_ttc_per_kwh
            total += period.fixed_ttc_per_day(day)
        result.append((day_b, round(total, 4)))
    return result, missing
