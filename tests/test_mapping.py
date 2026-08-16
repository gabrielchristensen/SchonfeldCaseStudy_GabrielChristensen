import pandas as pd
import pytest
import requests

from src.mapping import (
    candidate_cusips,
    cusip_to_ticker,
    fetch_openfigi_mappings,
    load_cusip_ticker_map,
    sp500_ticker_coverage,
    unmapped_summary,
)

_COLUMNS = ["CUSIP", "CIK", "FILING_DATE"]


def _panel(rows):
    df = pd.DataFrame(rows, columns=_COLUMNS)
    df["FILING_DATE"] = pd.to_datetime(df["FILING_DATE"])
    return df


def test_candidate_cusips_filters_by_breadth_threshold():
    panel = _panel([
        ("CUSIP1", "CIK_A", "2020-01-01"),
        ("CUSIP1", "CIK_B", "2020-01-01"),
        ("CUSIP2", "CIK_A", "2020-01-01"),
    ])
    result = candidate_cusips(panel, start_date="2020-01-01", end_date="2020-12-31", min_breadth=2)
    assert result == {"CUSIP1"}


def test_candidate_cusips_excludes_filings_outside_date_window():
    panel = _panel([
        ("CUSIP1", "CIK_A", "2019-01-01"),
        ("CUSIP1", "CIK_B", "2019-01-01"),
    ])
    result = candidate_cusips(panel, start_date="2020-01-01", end_date="2020-12-31", min_breadth=1)
    assert result == set()


def test_candidate_cusips_counts_distinct_ciks_not_rows():
    # Same CIK filing twice for the same CUSIP shouldn't inflate breadth.
    panel = _panel([
        ("CUSIP1", "CIK_A", "2020-01-01"),
        ("CUSIP1", "CIK_A", "2020-06-01"),
    ])
    result = candidate_cusips(panel, start_date="2020-01-01", end_date="2020-12-31", min_breadth=2)
    assert result == set()


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


def test_fetch_openfigi_mappings_picks_largest_composite_figi_group(monkeypatch):
    # Mirrors the real OpenFIGI response shape: a single CUSIP query returns
    # many global listings. The correct one is the largest same-compositeFIGI
    # group (here AAPL, 3 rows), not the smaller GOOGL-style noise (1 row).
    payload = [{"data": [
        {"ticker": "AAPL", "exchCode": "UA", "name": "APPLE INC", "compositeFIGI": "FIGI_AAPL",
         "marketSector": "Equity", "securityType2": "Common Stock"},
        {"ticker": "AAPL", "exchCode": "US", "name": "APPLE INC", "compositeFIGI": "FIGI_AAPL",
         "marketSector": "Equity", "securityType2": "Common Stock"},
        {"ticker": "AAPL", "exchCode": "GR", "name": "APPLE INC", "compositeFIGI": "FIGI_AAPL",
         "marketSector": "Equity", "securityType2": "Common Stock"},
        {"ticker": "UNRELATED", "exchCode": "XX", "name": "SOME OTHER CO", "compositeFIGI": "FIGI_OTHER",
         "marketSector": "Equity", "securityType2": "Common Stock"},
    ]}]
    monkeypatch.setattr("src.mapping.requests.post", lambda *a, **k: _FakeResponse(payload))

    result = fetch_openfigi_mappings(["037833100"])

    assert result.loc[0, "TICKER"] == "AAPL"
    assert result.loc[0, "RESOLVED"] == True


def test_fetch_openfigi_mappings_resolved_column_is_real_bool_dtype(monkeypatch):
    # Regression test: a fresh (no checkpoint) call's RESOLVED column was
    # silently demoted from bool to object dtype by pd.concat with the
    # always-created empty placeholder frame -- `~` on that object column
    # then did Python bitwise-invert (True -> -2, False -> -1) instead of
    # logical NOT, and pandas tried to use those as integer column labels,
    # raising a KeyError. Hit this for real on the very first production run.
    payload = [
        {"data": [{"ticker": "AAPL", "exchCode": "US", "name": "APPLE INC", "compositeFIGI": "F1"}]},
        {"error": "No identifier found."},
    ]
    monkeypatch.setattr("src.mapping.requests.post", lambda *a, **k: _FakeResponse(payload))
    monkeypatch.setattr("src.mapping.time.sleep", lambda s: None)

    result = fetch_openfigi_mappings(["037833100", "000000000"])

    assert result["RESOLVED"].dtype == bool
    # The exact operation that crashed in build_cusip_ticker_map:
    unresolved = result[~result["RESOLVED"]]
    assert unresolved["CUSIP"].tolist() == ["000000000"]


def test_fetch_openfigi_mappings_handles_no_us_exchange_row(monkeypatch):
    # Real case: ExxonMobil's CUSIP (30231G102) has no exchCode == "US" row
    # at all -- the real "XOM" listings use "UZ"/"OU"/"QU" instead, in a
    # 3-row group, versus an unrelated "EXMOC" listing in a 2-row group.
    # Naively falling back to the first result would pick "EXMOC".
    payload = [{"data": [
        {"ticker": "EXMOC", "exchCode": "CP", "name": "EXXON MOBIL CORP", "compositeFIGI": "FIGI_EXMOC",
         "marketSector": "Equity", "securityType2": "Common Stock"},
        {"ticker": "EXMOC", "exchCode": "RC", "name": "EXXON MOBIL CORP", "compositeFIGI": "FIGI_EXMOC",
         "marketSector": "Equity", "securityType2": "Common Stock"},
        {"ticker": "XOM", "exchCode": "UZ", "name": "EXXON MOBIL CORP", "compositeFIGI": "FIGI_XOM",
         "marketSector": "Equity", "securityType2": "Common Stock"},
        {"ticker": "XOM", "exchCode": "OU", "name": "EXXON MOBIL CORP", "compositeFIGI": "FIGI_XOM",
         "marketSector": "Equity", "securityType2": "Common Stock"},
        {"ticker": "XOM", "exchCode": "QU", "name": "EXXON MOBIL CORP", "compositeFIGI": "FIGI_XOM",
         "marketSector": "Equity", "securityType2": "Common Stock"},
    ]}]
    monkeypatch.setattr("src.mapping.requests.post", lambda *a, **k: _FakeResponse(payload))

    result = fetch_openfigi_mappings(["30231G102"])

    assert result.loc[0, "TICKER"] == "XOM"


def test_fetch_openfigi_mappings_marks_unresolved_without_dropping(monkeypatch):
    payload = [{"error": "No identifier found."}]
    monkeypatch.setattr("src.mapping.requests.post", lambda *a, **k: _FakeResponse(payload))

    result = fetch_openfigi_mappings(["000000000"])

    assert len(result) == 1
    assert result.loc[0, "RESOLVED"] == False
    assert pd.isna(result.loc[0, "TICKER"])


def test_fetch_openfigi_mappings_batches_at_10_when_unauthenticated(monkeypatch):
    # Verified empirically against the real endpoint: an unauthenticated
    # request is capped at exactly 10 mapping jobs ("Request may only
    # contain 10 mapping jobs." on an 11th).
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(len(json))
        return _FakeResponse([{"data": [{"ticker": "X", "exchCode": "US", "name": "X CORP"}]}] * len(json))

    monkeypatch.setattr("src.mapping.requests.post", fake_post)
    monkeypatch.setattr("src.mapping.time.sleep", lambda s: None)

    cusips = [f"CUSIP{i:04d}" for i in range(25)]
    result = fetch_openfigi_mappings(cusips)

    assert calls == [10, 10, 5]
    assert len(result) == 25


def test_fetch_openfigi_mappings_batches_at_100_when_authenticated(monkeypatch):
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(len(json))
        return _FakeResponse([{"data": [{"ticker": "X", "exchCode": "US", "name": "X CORP"}]}] * len(json))

    monkeypatch.setattr("src.mapping.requests.post", fake_post)
    monkeypatch.setattr("src.mapping.time.sleep", lambda s: None)

    cusips = [f"CUSIP{i:04d}" for i in range(250)]
    result = fetch_openfigi_mappings(cusips, api_key="secret-key")

    assert calls == [100, 100, 50]
    assert len(result) == 250


def test_fetch_openfigi_mappings_sends_api_key_header(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured["headers"] = headers
        return _FakeResponse([{"data": [{"ticker": "X", "exchCode": "US", "name": "X CORP"}]}])

    monkeypatch.setattr("src.mapping.requests.post", fake_post)

    fetch_openfigi_mappings(["037833100"], api_key="secret-key")

    assert captured["headers"]["X-OPENFIGI-APIKEY"] == "secret-key"


def test_fetch_openfigi_mappings_retries_once_on_429(monkeypatch):
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(1)
        if len(calls) == 1:
            return _FakeResponse({}, status_code=429)
        return _FakeResponse([{"data": [{"ticker": "X", "exchCode": "US", "name": "X CORP"}]}])

    monkeypatch.setattr("src.mapping.requests.post", fake_post)
    monkeypatch.setattr("src.mapping.time.sleep", lambda s: None)

    result = fetch_openfigi_mappings(["037833100"])

    assert len(calls) == 2
    assert result.loc[0, "RESOLVED"] == True


def test_fetch_openfigi_mappings_retries_on_connection_error(monkeypatch):
    # A real "No route to host" transient failure killed an unattended
    # ~49-minute build with no retry -- this is the regression guard.
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise requests.exceptions.ConnectionError("No route to host")
        return _FakeResponse([{"data": [{"ticker": "X", "exchCode": "US", "name": "X CORP"}]}])

    monkeypatch.setattr("src.mapping.requests.post", fake_post)
    monkeypatch.setattr("src.mapping.time.sleep", lambda s: None)

    result = fetch_openfigi_mappings(["037833100"])

    assert len(calls) == 2
    assert result.loc[0, "RESOLVED"] == True


def test_fetch_openfigi_mappings_raises_after_exhausting_retries(monkeypatch):
    def fake_post(url, headers, json, timeout):
        raise requests.exceptions.ConnectionError("No route to host")

    monkeypatch.setattr("src.mapping.requests.post", fake_post)
    monkeypatch.setattr("src.mapping.time.sleep", lambda s: None)

    with pytest.raises(requests.exceptions.ConnectionError):
        fetch_openfigi_mappings(["037833100"])


def test_fetch_openfigi_mappings_checkpoint_resumes_after_failure(tmp_path, monkeypatch):
    checkpoint_path = tmp_path / "progress.csv"
    calls = []
    cusip2_attempts = [0]

    def flaky_post(url, headers, json, timeout):
        calls.append([j["idValue"] for j in json])
        if json[0]["idValue"] == "CUSIP2":
            cusip2_attempts[0] += 1
            # Fails for exactly _post_with_retry's max_attempts (5), so the
            # first fetch_openfigi_mappings call exhausts its retries and
            # the failure propagates -- then succeeds on the resume call,
            # simulating a transient outage that resolved in the meantime.
            if cusip2_attempts[0] <= 5:
                raise requests.exceptions.ConnectionError("No route to host")
        return _FakeResponse([{"data": [{"ticker": f"T{j['idValue']}", "exchCode": "US", "name": "X"}]} for j in json])

    monkeypatch.setattr("src.mapping.requests.post", flaky_post)
    monkeypatch.setattr("src.mapping.time.sleep", lambda s: None)
    # Small batch size so 3 CUSIPs span 3 separate requests, letting the
    # 2nd one fail without touching the 1st or 3rd.
    monkeypatch.setattr("src.mapping._BATCH_SIZE_UNAUTHENTICATED", 1)

    cusips = ["CUSIP1", "CUSIP2", "CUSIP3"]
    with pytest.raises(requests.exceptions.ConnectionError):
        fetch_openfigi_mappings(cusips, checkpoint_path=checkpoint_path)

    # First CUSIP's result was checkpointed before the failure.
    partial = pd.read_csv(checkpoint_path, dtype={"CUSIP": str})
    assert partial["CUSIP"].tolist() == ["CUSIP1"]

    # Resuming skips CUSIP1 (already checkpointed) and completes the rest.
    calls.clear()
    result = fetch_openfigi_mappings(cusips, checkpoint_path=checkpoint_path)

    assert "CUSIP1" not in [c for batch in calls for c in batch]
    assert sorted(result["CUSIP"].tolist()) == cusips


def test_load_cusip_ticker_map_missing_file_raises_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="python -m src.mapping --build"):
        load_cusip_ticker_map(map_path=tmp_path / "missing.csv", overrides_path=tmp_path / "missing_overrides.csv")


def test_load_cusip_ticker_map_override_wins_on_conflict(tmp_path):
    map_path = tmp_path / "cusip_ticker_map.csv"
    overrides_path = tmp_path / "cusip_overrides.csv"
    pd.DataFrame({"CUSIP": ["037833100"], "TICKER": ["OLDTICK"], "NAME": ["OLD NAME"]}).to_csv(map_path, index=False)
    pd.DataFrame({
        "CUSIP": ["037833100"], "TICKER": ["AAPL"], "NAME": ["APPLE INC"], "REASON": ["ticker change"],
    }).to_csv(overrides_path, index=False)

    result = load_cusip_ticker_map(map_path=map_path, overrides_path=overrides_path)

    assert result.set_index("CUSIP").loc["037833100", "TICKER"] == "AAPL"


def test_load_cusip_ticker_map_supports_multiple_ticker_aliases_per_cusip(tmp_path):
    # Real case: OpenFIGI resolves a CUSIP to its CURRENT ticker only, so
    # Facebook's CUSIP (30303M102) resolves to "META" (its 2022 rename) but
    # never to "FB", the retired label older S&P 500 membership rows still
    # use. The overrides file lists BOTH aliases explicitly (overrides fully
    # supersede the base map's single row for that CUSIP, they don't merge
    # with it), so universe.sp500_members() can resolve either ticker
    # string to the right CUSIP.
    map_path = tmp_path / "cusip_ticker_map.csv"
    overrides_path = tmp_path / "cusip_overrides.csv"
    pd.DataFrame({"CUSIP": ["30303M102"], "TICKER": ["META"], "NAME": ["META PLATFORMS INC"]}).to_csv(
        map_path, index=False
    )
    pd.DataFrame({
        "CUSIP": ["30303M102", "30303M102"],
        "TICKER": ["META", "FB"],
        "NAME": ["META PLATFORMS INC", "FACEBOOK INC"],
        "REASON": ["current ticker", "retired pre-2022 ticker"],
    }).to_csv(overrides_path, index=False)

    result = load_cusip_ticker_map(map_path=map_path, overrides_path=overrides_path)

    tickers_for_cusip = set(result.loc[result["CUSIP"] == "30303M102", "TICKER"])
    assert tickers_for_cusip == {"META", "FB"}


def test_load_cusip_ticker_map_works_without_overrides_file(tmp_path):
    map_path = tmp_path / "cusip_ticker_map.csv"
    pd.DataFrame({"CUSIP": ["037833100"], "TICKER": ["AAPL"], "NAME": ["APPLE INC"]}).to_csv(map_path, index=False)

    result = load_cusip_ticker_map(map_path=map_path, overrides_path=tmp_path / "does_not_exist.csv")

    assert len(result) == 1


def test_cusip_to_ticker_maps_known_and_nan_for_unknown():
    mapping = pd.DataFrame({"CUSIP": ["037833100"], "TICKER": ["AAPL"], "NAME": ["APPLE INC"]})
    cusips = pd.Series(["037833100", "999999999"])

    result = cusip_to_ticker(cusips, mapping)

    assert result.iloc[0] == "AAPL"
    assert pd.isna(result.iloc[1])


def test_cusip_to_ticker_prefers_current_ticker_over_retired_alias():
    # Mirrors cusip_overrides.csv's real, authored row order for a renamed
    # company: current ticker listed first, retired pre-rename alias second
    # (needed by sp500_members() for historical membership-row matching).
    # cusip_to_ticker must resolve to the current ticker, not whichever row
    # happens to sort last -- verified against real Yahoo Finance data that
    # a retired post-rename ticker like "FB" or "BK" has zero price history.
    mapping = pd.DataFrame({
        "CUSIP": ["30303M102", "30303M102"],
        "TICKER": ["META", "FB"],
        "NAME": ["META PLATFORMS INC", "FACEBOOK INC"],
    })

    result = cusip_to_ticker(pd.Series(["30303M102"]), mapping)

    assert result.iloc[0] == "META"


def test_unmapped_summary_counts_and_lists_top_unmapped():
    mapping = pd.DataFrame({"CUSIP": ["037833100"], "TICKER": ["AAPL"], "NAME": ["APPLE INC"]})
    cusips = pd.Series(["037833100", "999999999", "999999999", "888888888"])

    summary = unmapped_summary(cusips, mapping)

    assert summary["n_total"] == 4
    assert summary["n_unmapped"] == 3
    assert summary["frac_unmapped"] == 0.75
    assert summary["top_unmapped"]["999999999"] == 2


def test_sp500_ticker_coverage_finds_missing_tickers(tmp_path):
    history_path = tmp_path / "sp500_history.csv"
    pd.DataFrame({"date": ["2020-01-01"], "tickers": ["AAPL,MSFT,GOOGL"]}).to_csv(history_path, index=False)
    mapping = pd.DataFrame({
        "CUSIP": ["037833100", "594918104"],
        "TICKER": ["AAPL", "MSFT"],
        "NAME": ["APPLE INC", "MICROSOFT CORP"],
    })

    coverage = sp500_ticker_coverage(mapping, history_path)

    assert coverage["n_ever_sp500_tickers"] == 3
    assert coverage["missing_tickers"] == ["GOOGL"]
    assert coverage["frac_missing"] == pytest.approx(1 / 3)
