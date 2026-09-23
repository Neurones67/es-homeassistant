"""Tests du parseur CSV ES, sur des exports au format réel d'ES.

Les CSV d'exemple sont synthétiques : voir scripts/generate_sample_csv.py.

Lançable sans Home Assistant :

    python3 -m pytest tests/ -q
    # ou simplement
    python3 tests/test_csv_parser.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

# Permet d'importer le module sans installer le package.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "custom_components" / "es_strasbourg"))

from csv_parser import ESTIMATED, REAL, parse_csv  # noqa: E402

SAMPLE = ROOT / "export_consommations_energie.csv"
# Export multi-couples (Consommation;Index)x2, cas d'un changement de compteur.
SAMPLE_MULTI = ROOT / "Consommation_Journaliere.csv"


def _load() -> list:
    return parse_csv(SAMPLE.read_bytes())


def test_row_count_and_bounds() -> None:
    records = _load()
    assert len(records) == 351
    assert records[0].day == date(2025, 8, 1)
    assert records[0].index_kwh == 20000
    assert records[0].nature == REAL
    assert records[-1].day == date(2026, 7, 17)
    assert records[-1].index_kwh == 23609


def test_last_row_has_no_consumption() -> None:
    # Le jour courant n'a pas encore de consommation, mais a un index.
    last = _load()[-1]
    assert last.consumption_kwh is None
    assert last.index_kwh == 23609


def test_thousands_separator_stripped() -> None:
    # "20 007" doit devenir 20007.
    records = _load()
    assert records[1].index_kwh == 20007


def test_estimated_vs_real_detected() -> None:
    records = _load()
    natures = {r.nature for r in records}
    assert natures == {REAL, ESTIMATED}
    # 12-08-2025 est une ligne "estimée" dans l'échantillon.
    by_day = {r.day: r for r in records}
    assert by_day[date(2025, 8, 12)].nature == ESTIMATED
    assert by_day[date(2025, 8, 1)].nature == REAL


def test_index_matches_consumption_delta() -> None:
    # Invariant clé : consommation(D) == index(D+1) - index(D).
    records = _load()
    for cur, nxt in zip(records, records[1:]):
        if cur.consumption_kwh is None or cur.index_kwh is None or nxt.index_kwh is None:
            continue
        assert nxt.index_kwh - cur.index_kwh == cur.consumption_kwh, cur.day


def test_parses_utf8_and_text_input() -> None:
    # Le parseur doit accepter du texte déjà décodé et de l'UTF-8.
    text = SAMPLE.read_bytes().decode("cp1252")
    from_text = parse_csv(text)
    from_utf8 = parse_csv(text.encode("utf-8"))
    assert len(from_text) == len(from_utf8) == 351


def test_no_date_gaps() -> None:
    records = _load()
    for cur, nxt in zip(records, records[1:]):
        assert (nxt.day - cur.day).days == 1, f"trou entre {cur.day} et {nxt.day}"


def test_multi_pair_export_all_indexes_read() -> None:
    # Export à deux couples (Consommation;Index) : chaque jour doit avoir un
    # index, y compris après la bascule vers le 2e couple de colonnes.
    records = parse_csv(SAMPLE_MULTI.read_bytes())
    assert len(records) == 660
    assert all(r.index_kwh is not None for r in records[:-1])
    assert records[0].day == date(2024, 9, 26)
    assert records[0].index_kwh == 5000
    # Dernier jour : conso encore vide mais index présent.
    assert records[-1].day == date(2026, 7, 17)
    assert records[-1].consumption_kwh is None
    assert records[-1].index_kwh == 11794


def test_multi_pair_switch_is_continuous() -> None:
    # La bascule 1er -> 2e couple (14-03 -> 15-03-2025) garde l'index continu et
    # respecte l'invariant conso(D) == index(D+1) - index(D).
    by_day = {r.day: r for r in parse_csv(SAMPLE_MULTI.read_bytes())}
    d3, d4, d5 = (by_day[date(2025, 3, day)] for day in (13, 14, 15))
    assert (d4.index_kwh, d5.index_kwh) == (7201, 7212)
    assert d4.index_kwh - d3.index_kwh == d3.consumption_kwh
    assert d5.index_kwh - d4.index_kwh == d4.consumption_kwh


def test_multi_pair_invariant_and_monotonic() -> None:
    records = parse_csv(SAMPLE_MULTI.read_bytes())
    for cur, nxt in zip(records, records[1:]):
        assert nxt.index_kwh >= cur.index_kwh  # index cumulé croissant
        if cur.consumption_kwh is None:
            continue
        assert nxt.index_kwh - cur.index_kwh == cur.consumption_kwh, cur.day


def test_multi_pair_nc_consumption_keeps_index() -> None:
    # « NC » en consommation : valeur absente, mais l'index reste exploitable.
    by_day = {r.day: r for r in parse_csv(SAMPLE_MULTI.read_bytes())}
    nc = by_day[date(2025, 6, 3)]
    assert (nc.consumption_kwh, nc.index_kwh) == (None, 7997)
    assert by_day[date(2025, 6, 5)].index_kwh == 8007


def test_nc_values_treated_as_missing() -> None:
    # ES écrit "NC" (non communiqué) pour une valeur manquante (vu dans un mail réel).
    text = (
        "Date;Nature relève;Consommation (kWh);Index;;\n"
        "01-09-2026;evt réelle;NC;13 500;;\n"
        "02-09-2026;evt réelle;NC;NC;;\n"
        "03-09-2026;evt réelle;7;13 520;;\n"
    )
    records = parse_csv(text)
    assert [(r.consumption_kwh, r.index_kwh) for r in records] == [
        (None, 13500),
        (None, None),
        (7, 13520),
    ]


if __name__ == "__main__":
    # Exécution directe sans pytest.
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} tests passés.")
