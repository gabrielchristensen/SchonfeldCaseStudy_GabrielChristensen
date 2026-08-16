import pandas as pd
import pytest

from src.universe import (
    build_sp500_history,
    passive_manager_ciks,
    sp500_members,
    sp500_universe_breadth,
)

PERIOD = "2020-03-31"

_PANEL_COLUMNS = [
    "ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK",
    "PERIODOFREPORT", "CUSIP", "VALUE", "SSHPRNAMT",
]


def _panel(rows):
    df = pd.DataFrame(rows, columns=_PANEL_COLUMNS)
    df["FILING_DATE"] = pd.to_datetime(df["FILING_DATE"])
    df["PERIODOFREPORT"] = pd.to_datetime(df["PERIODOFREPORT"])
    return df


def _mapping():
    return pd.DataFrame({
        "CUSIP": ["037833100", "594918104", "02079K107"],
        "TICKER": ["AAPL", "MSFT", "GOOGL"],
        "NAME": ["APPLE INC", "MICROSOFT CORP", "ALPHABET INC"],
    })


def _write_history(path, rows):
    pd.DataFrame(rows, columns=["date", "tickers"]).to_csv(path, index=False)


class _FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def test_sp500_members_takes_last_row_on_or_before_as_of_date(tmp_path):
    history_path = tmp_path / "sp500_history.csv"
    _write_history(history_path, [
        ("2015-01-01", "AAPL,MSFT"),
        ("2020-06-01", "AAPL,MSFT,GOOGL"),
    ])

    members = sp500_members("2020-01-01", _mapping(), history_path=history_path)

    assert members == {"037833100", "594918104"}


def test_sp500_members_picks_up_membership_change_on_exact_date(tmp_path):
    history_path = tmp_path / "sp500_history.csv"
    _write_history(history_path, [
        ("2015-01-01", "AAPL,MSFT"),
        ("2020-06-01", "AAPL,MSFT,GOOGL"),
    ])

    members = sp500_members("2020-06-01", _mapping(), history_path=history_path)

    assert members == {"037833100", "594918104", "02079K107"}


def test_sp500_members_raises_when_as_of_date_predates_history(tmp_path):
    history_path = tmp_path / "sp500_history.csv"
    _write_history(history_path, [("2015-01-01", "AAPL,MSFT")])

    with pytest.raises(ValueError, match="predates"):
        sp500_members("2010-01-01", _mapping(), history_path=history_path)


def test_sp500_members_excludes_tickers_with_no_resolved_cusip(tmp_path):
    history_path = tmp_path / "sp500_history.csv"
    _write_history(history_path, [("2015-01-01", "AAPL,UNMAPPEDTICKER")])

    members = sp500_members("2020-01-01", _mapping(), history_path=history_path)

    assert members == {"037833100"}


def test_passive_manager_ciks_zfills_hand_edited_values(tmp_path):
    path = tmp_path / "passive_manager_ciks.csv"
    pd.DataFrame({
        "CIK": ["1364742", "0000102909"],
        "MANAGER_NAME": ["Vanguard Group Inc", "BlackRock Inc"],
        "NOTES": ["", ""],
    }).to_csv(path, index=False)

    ciks = passive_manager_ciks(path)

    assert ciks == {"0001364742", "0000102909"}


def test_passive_manager_ciks_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        passive_manager_ciks(path="does_not_exist.csv")


def test_passive_manager_ciks_covers_both_eras_of_known_cik_transitions():
    # Regression guard for a real bug: the first cut of this file was built
    # from a single (2026) quarter and only captured each manager's CURRENT
    # CIK. Verified against real data at 2015/2019/2023/2026 that Vanguard
    # and BlackRock each filed under a *different* CIK for most of the
    # 2013-2025 backtest window -- silently making the exclusion a no-op
    # for ~13 of ~13.5 years. This checks the real committed file (not a
    # synthetic fixture) so a future edit can't reintroduce a single-era
    # snapshot without this test catching it.
    ciks = passive_manager_ciks()

    # Vanguard: pre-2026 dominant CIK and 2026-era dominant CIKs.
    assert "0000102909" in ciks  # VANGUARD GROUP INC, dominant 2013-2025
    assert "0002100119" in ciks  # VANGUARD CAPITAL MANAGEMENT LLC, dominant 2026+

    # BlackRock: pre-2026 dominant CIK and 2026-era dominant CIK.
    assert "0001364742" in ciks  # BlackRock Inc., dominant ~2019-2023
    assert "0002012383" in ciks  # BlackRock, Inc., dominant 2026+


def test_sp500_universe_breadth_restricts_to_universe_and_excludes_passive(tmp_path):
    history_path = tmp_path / "sp500_history.csv"
    _write_history(history_path, [("2020-01-01", "AAPL,MSFT")])

    panel = _panel([
        # CIK_A's one filing covers all three of its CUSIPs -- a real 13F
        # filing reports every holding under a single accession number.
        ("A1", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "037833100", 100, 10),
        ("A1", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "594918104", 100, 10),
        # GOOGL: not in the S&P 500 history fixture above -- must be excluded.
        ("A1", "2020-05-10", "13F-HR", "CIK_A", PERIOD, "02079K107", 100, 10),
        # AAPL: two more filers, one of them passive -- should count 2 total.
        ("A2", "2020-05-10", "13F-HR", "CIK_B", PERIOD, "037833100", 100, 10),
        ("A3", "2020-05-10", "13F-HR", "CIK_PASSIVE", PERIOD, "037833100", 100, 10),
    ])

    result = sp500_universe_breadth(
        panel, PERIOD, as_of_date="2020-06-01", mapping=_mapping(),
        passive_ciks={"CIK_PASSIVE"}, history_path=history_path,
    )

    breadth_by_cusip = result.set_index("CUSIP")["breadth"].to_dict()
    assert breadth_by_cusip == {"037833100": 2, "594918104": 1}


def test_build_sp500_history_trims_window_and_dedups_consecutive_rows(tmp_path, monkeypatch):
    csv_text = (
        "date,tickers\n"
        '2005-01-01,"AAPL,MSFT"\n'
        '2015-01-01,"AAPL,MSFT"\n'
        '2015-06-01,"AAPL,MSFT"\n'  # identical membership -- should be dropped
        '2020-01-01,"AAPL,MSFT,GOOGL"\n'
    )
    monkeypatch.setattr("src._http.requests.get", lambda *a, **k: _FakeResponse(csv_text))
    out_path = tmp_path / "sp500_history.csv"

    result = build_sp500_history(start_date="2010-01-01", out_path=out_path)

    assert result["date"].min() == pd.Timestamp("2015-01-01")
    assert len(result) == 2  # 2015-01-01 kept, 2015-06-01 dropped as a duplicate, 2020-01-01 kept
    assert out_path.exists()
