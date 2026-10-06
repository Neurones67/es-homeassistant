"""Tests du barème tarif bleu et du calcul de coût (sans Home Assistant).

    python3 -m pytest tests/test_tariff.py -q
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "custom_components" / "es_strasbourg"))

from tariff import TariffTable, compute_cumulative_cost, parse_tariff_csv  # noqa: E402

EMBEDDED = ROOT / "custom_components" / "es_strasbourg" / "data" / "cre_option_base.csv"

# Extrait au format CRE : en-tête, période close, période en cours (fin vide).
SAMPLE = (
    "DATE_DEBUT;DATE_FIN;P_SOUSCRITE;PART_FIXE_HT;PART_FIXE_TTC;"
    "PART_VARIABLE_HT;PART_VARIABLE_TTC\r\n"
    "01/02/2026;31/07/2026;6;141,6;188,2;0,1308;0,1940\r\n"
    "01/08/2026;;6;143,28;190,81;0,1361;0,2001\r\n"
    "01/08/2026;;9;178,8;239,84;0,1348;0,1985\r\n"
)


@pytest.fixture
def table() -> TariffTable:
    return TariffTable(parse_tariff_csv(SAMPLE))


def test_parse_decimal_comma_and_open_end() -> None:
    periods = parse_tariff_csv(SAMPLE.encode())
    assert len(periods) == 3
    current = periods[1]
    assert (current.start, current.end, current.power_kva) == (date(2026, 8, 1), None, 6)
    assert (current.fixed_ttc_per_year, current.energy_ttc_per_kwh) == (190.81, 0.2001)


def test_period_lookup_by_day_and_power(table: TariffTable) -> None:
    assert table.period_for(date(2026, 7, 31), 6).energy_ttc_per_kwh == 0.1940
    assert table.period_for(date(2026, 8, 1), 6).energy_ttc_per_kwh == 0.2001
    assert table.period_for(date(2030, 1, 1), 6).energy_ttc_per_kwh == 0.2001  # en cours
    assert table.period_for(date(2026, 7, 31), 9) is None  # pas de tarif avant le 01/08
    assert table.period_for(date(2026, 8, 1), 3) is None  # puissance absente
    assert table.powers == [6, 9]


def test_fixed_part_prorated_per_day(table: TariffTable) -> None:
    period = table.period_for(date(2026, 8, 1), 6)
    assert period.fixed_ttc_per_day(date(2026, 8, 1)) == pytest.approx(190.81 / 365)
    assert period.fixed_ttc_per_day(date(2028, 8, 1)) == pytest.approx(190.81 / 366)


def test_cumulative_cost_across_tariff_change(table: TariffTable) -> None:
    # 10 kWh le 31/07 (ancien tarif), puis 30 kWh sur 3 jours (relevés « NC »).
    points = [(date(2026, 7, 31), 100), (date(2026, 8, 1), 110), (date(2026, 8, 4), 140)]
    cumulative, missing = compute_cumulative_cost(points, table, 6)

    day_jul31 = 10 * 0.1940 + 188.2 / 365
    day_aug = 10 * 0.2001 + 190.81 / 365
    assert [day for day, _ in cumulative] == [p[0] for p in points]
    assert cumulative[0][1] == 0
    assert cumulative[1][1] == pytest.approx(day_jul31, abs=1e-4)
    assert cumulative[2][1] == pytest.approx(day_jul31 + 3 * day_aug, abs=1e-4)
    assert missing == []


def test_days_without_tariff_reported(table: TariffTable) -> None:
    points = [(date(2026, 7, 30), 0), (date(2026, 8, 2), 30)]
    cumulative, missing = compute_cumulative_cost(points, table, 9)
    assert missing == [date(2026, 7, 30), date(2026, 7, 31)]
    assert cumulative[-1][1] == pytest.approx(10 * 0.1985 + 239.84 / 365, abs=1e-4)


def test_invalid_csv_rejected() -> None:
    with pytest.raises(ValueError, match="Colonnes absentes"):
        parse_tariff_csv("<html>Maintenance</html>")
    with pytest.raises(ValueError, match="Aucun tarif"):
        parse_tariff_csv(SAMPLE.splitlines()[0])


def test_embedded_copy_is_valid() -> None:
    table = TariffTable(parse_tariff_csv(EMBEDDED.read_bytes()))
    assert table.powers == [3, 6, 9, 12, 15]
    # Couvre en continu l'historique ES de l'exemple (depuis 2024).
    for day in (date(2024, 9, 26), date(2025, 2, 1), date(2026, 9, 23)):
        assert table.period_for(day, 6) is not None
