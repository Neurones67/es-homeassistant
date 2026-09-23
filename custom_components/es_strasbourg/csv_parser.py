"""Parsing des exports CSV de consommation d'Électricité de Strasbourg.

Ce module est volontairement sans dépendance à Home Assistant afin de pouvoir
être testé de façon isolée.

Format observé (option Base, tarif simple) :

    ;;FR Base FR Base;FR Base FR Base;;...
    Date;Nature relève;Consommation (kWh);Index;;...
    01-08-2025;evt réelle;7;20 000;;...
    02-08-2025;estimée;9;20 007;;...
    ...
    17-07-2026;evt réelle;;23 609;;...   <-- dernier jour : conso vide, index présent

Certains exports (ex. « Consommation_Journaliere.csv ») comportent **plusieurs
couples ``Consommation;Index``** côte à côte — typiquement après un changement de
compteur : les données basculent d'un couple au suivant à une date donnée, et
l'``Index`` reste continu d'un couple à l'autre. ::

    Date;Nature relève;Consommation (kWh);Index;Consommation (kWh);Index;...
    14-03-2025;estimée;11;7 201;;;...     <-- 1er couple
    15-03-2025;evt réelle;;;13;7 212;...    <-- bascule sur le 2e couple

On détecte donc dynamiquement les couples depuis l'en-tête et, pour chaque jour,
on retient le premier couple dont l'``Index`` est renseigné.

Points importants :
* séparateur ``;`` ;
* encodage Windows-1252 (cp1252) le plus souvent, parfois UTF-8 ;
* la colonne ``Index`` est un relevé de compteur **cumulé** en kWh, avec un
  espace (ou espace insécable) comme séparateur de milliers : ``20 007`` = 20007 ;
* ``Consommation(D) == Index(D+1) - Index(D)`` (vérifié sur les données réelles) ;
  on n'utilise donc que l'``Index`` pour alimenter les statistiques cumulées de HA.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime

__all__ = ["ConsumptionRecord", "parse_csv", "REAL", "ESTIMATED"]

REAL = "real"
ESTIMATED = "estimated"

# Caractères parasites à retirer d'un nombre : espace normal, espace insécable,
# espace insécable fine.
_NUMBER_STRIP = str.maketrans("", "", "   ")


@dataclass(frozen=True)
class ConsumptionRecord:
    """Une ligne de consommation journalière."""

    day: date
    nature: str  # REAL ou ESTIMATED
    consumption_kwh: int | None  # delta du jour, None si absent (jour courant)
    index_kwh: int | None  # relevé de compteur cumulé, None si absent


def _decode(data: bytes) -> str:
    """Décode les octets bruts en essayant UTF-8 puis cp1252 (repli sûr)."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    # Dernier repli : latin-1 ne lève jamais d'erreur.
    return data.decode("latin-1")


def _parse_number(raw: str) -> int | None:
    """Convertit ``"20 007"`` en ``20007``.

    Renvoie None si la cellule est vide ou non numérique (ES écrit ``NC``,
    « non communiqué », pour une valeur manquante).
    """
    cleaned = raw.translate(_NUMBER_STRIP).strip()
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _parse_nature(raw: str) -> str:
    """Normalise la nature du relevé (tolère les artefacts d'encodage).

    Ex. ``"estimée"``, ``"estim�e"`` -> ESTIMATED ; ``"evt réelle"`` -> REAL.
    """
    return ESTIMATED if "estim" in raw.strip().lower() else REAL


def _cell(row: list[str], col: int) -> str:
    """Renvoie la cellule ``col`` de ``row`` ou ``""`` si hors limites."""
    return row[col] if 0 <= col < len(row) else ""


def _column_pairs(header: list[str]) -> list[tuple[int, int]]:
    """Repère les couples ``(colonne Consommation, colonne Index)`` de l'en-tête.

    Un export standard n'a qu'un couple ``(2, 3)`` ; un export post-changement de
    compteur en aligne plusieurs (``(2, 3)``, ``(4, 5)``, ...). On identifie
    chaque colonne « Index » et on lui associe la colonne « Consommation » qui la
    précède immédiatement. Repli sur ``[(2, 3)]`` si l'en-tête est inattendu.
    """
    pairs: list[tuple[int, int]] = []
    for col, cell in enumerate(header):
        if cell.strip().lower() == "index" and col >= 3:
            pairs.append((col - 1, col))
    return pairs or [(2, 3)]


def parse_csv(content: bytes | str) -> list[ConsumptionRecord]:
    """Parse un export CSV ES et renvoie la liste des relevés journaliers.

    Args:
        content: contenu du CSV, en octets (recommandé, l'encodage est détecté)
            ou en texte déjà décodé.

    Returns:
        Liste de :class:`ConsumptionRecord` triée par date croissante. Seules les
        lignes possédant une date valide sont conservées.
    """
    text = _decode(content) if isinstance(content, bytes) else content

    reader = csv.reader(io.StringIO(text), delimiter=";")
    records: list[ConsumptionRecord] = []
    pairs: list[tuple[int, int]] | None = None

    for row in reader:
        if not row or not row[0].strip():
            continue

        first = row[0].strip()

        # On saute tout jusqu'à la ligne d'en-tête "Date;Nature relève;...", dont
        # on déduit la position des couples (Consommation, Index).
        if pairs is None:
            if first.lower() == "date":
                pairs = _column_pairs(row)
            continue

        # Ligne de données : au minimum Date;Nature;Conso;Index.
        if len(row) < 4:
            continue

        try:
            day = datetime.strptime(first, "%d-%m-%Y").date()
        except ValueError:
            # Ligne inattendue (pied de page, etc.) : on l'ignore.
            continue

        # On retient le premier couple dont l'Index est renseigné ; à défaut, la
        # première Consommation trouvée (jour courant sans index, rare).
        consumption_kwh: int | None = None
        index_kwh: int | None = None
        for conso_col, index_col in pairs:
            idx = _parse_number(_cell(row, index_col))
            if idx is not None:
                index_kwh = idx
                consumption_kwh = _parse_number(_cell(row, conso_col))
                break
        else:
            for conso_col, _ in pairs:
                consumption_kwh = _parse_number(_cell(row, conso_col))
                if consumption_kwh is not None:
                    break

        records.append(
            ConsumptionRecord(
                day=day,
                nature=_parse_nature(row[1]),
                consumption_kwh=consumption_kwh,
                index_kwh=index_kwh,
            )
        )

    records.sort(key=lambda r: r.day)
    return records
