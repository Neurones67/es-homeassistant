#!/usr/bin/env python3
"""Génère les CSV d'exemple, avec des données **synthétiques**.

Les fichiers reproduisent fidèlement le format des exports d'Électricité de
Strasbourg (cp1252, CRLF, 20 champs par ligne, ``20 007`` pour 20007, relevés
« evt réelle » / « estimée », ``NC``…), mais les consommations sont inventées :
profil saisonnier + bruit aléatoire à graine fixe (sortie reproductible).

    python3 scripts/generate_sample_csv.py

Fichiers produits à la racine du dépôt :
  * export_consommations_energie.csv : un couple (Consommation;Index) ;
  * Consommation_Journaliere.csv : deux couples, bascule du 1er au 2e couple
    au 15-03-2025 (cas d'un changement de compteur), et quelques ``NC``.
"""

from __future__ import annotations

from datetime import date, timedelta
import math
from pathlib import Path
import random

ROOT = Path(__file__).resolve().parent.parent
FIELDS = 20
REAL, ESTIMATED = "evt réelle", "estimée"

# Points fixes, utilisés par les tests.
SINGLE_START, SINGLE_END, SINGLE_INDEX = date(2025, 8, 1), date(2026, 7, 17), 20000
MULTI_START, MULTI_END, MULTI_INDEX = date(2024, 9, 26), date(2026, 7, 17), 5000
MULTI_SWITCH = date(2025, 3, 15)
FORCED_NATURE = {date(2025, 8, 1): REAL, date(2025, 8, 12): ESTIMATED}
MULTI_NC = {date(2025, 6, 3), date(2025, 6, 4)}  # conso « NC », index présent


def _number(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _row(cells: list[str]) -> str:
    return ";".join(cells + [""] * (FIELDS - len(cells)))


def _daily_kwh(rng: random.Random, day: date) -> int:
    """Profil de chauffage électrique : ~15 kWh en hiver, ~4 kWh en été."""
    season = math.cos(2 * math.pi * (day.timetuple().tm_yday - 20) / 365)
    weekend = 1.5 if day.weekday() >= 5 else 0.0
    return max(1, round(9.5 + 5.5 * season + weekend + rng.gauss(0, 2)))


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _series(seed: int, start: date, end: date, first_index: int):
    """(jour, nature, conso ou None, index) ; conso(J) = index(J+1) - index(J)."""
    rng = random.Random(seed)
    days = _days(start, end)
    index = first_index
    for day in days:
        nature = FORCED_NATURE.get(day) or (ESTIMATED if rng.random() < 0.3 else REAL)
        conso = None if day == end else _daily_kwh(rng, day)
        yield day, nature, conso, index
        index += conso or 0


def _write(name: str, lines: list[str]) -> None:
    """Écrit comme ES : cp1252, fins de ligne CRLF."""
    (ROOT / name).write_bytes(("\r\n".join(lines) + "\r\n").encode("cp1252"))
    print(f"{name} : {len(lines) - 2} jours")


def generate_single() -> None:
    lines = [
        _row(["", "", "FR Base FR Base", "FR Base FR Base"]),
        _row(["Date", "Nature relève", "Consommation (kWh)", "Index"]),
    ]
    for day, nature, conso, index in _series(1, SINGLE_START, SINGLE_END, SINGLE_INDEX):
        conso_cell = "" if conso is None else str(conso)
        lines.append(_row([day.strftime("%d-%m-%Y"), nature, conso_cell, _number(index)]))
    _write("export_consommations_energie.csv", lines)


def generate_multi() -> None:
    pair = ["Consommation (kWh)", "Index"]
    lines = [
        _row(["", ""] + ["FR Base FR Base"] * 4),
        _row(["Date", "Nature relève", *pair, *pair]),
    ]
    for day, nature, conso, index in _series(2, MULTI_START, MULTI_END, MULTI_INDEX):
        conso_cell = "NC" if day in MULTI_NC else ("" if conso is None else str(conso))
        cells = [conso_cell, _number(index)]
        # Avant la bascule : 1er couple rempli ; ensuite : 2e couple.
        values = cells + ["", ""] if day < MULTI_SWITCH else ["", ""] + cells
        lines.append(_row([day.strftime("%d-%m-%Y"), nature, *values]))
    _write("Consommation_Journaliere.csv", lines)


if __name__ == "__main__":
    generate_single()
    generate_multi()
