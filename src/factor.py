"""Ownership breadth momentum factor.

signal(cusip, period) = breadth(period) - breadth(prior quarter), computed
per formation date and cross-sectionally standardized within the S&P 500
universe that date. Grounded in Chen-Hong-Kubik (2002): rising breadth
relaxes short-sale/difference-of-opinion constraints (Miller 1977),
predicting positive future returns.

Point-in-time design: both breadth(period) and breadth(prior quarter) are
evaluated as of the SAME as_of_date (the current formation date), not a
frozen snapshot of the prior quarter as of its own earlier formation date.
This is deliberate, not an oversight -- by the current formation date,
everything about the prior quarter is already fully public regardless of
how complete it was back at its own formation date, so using the freshest
available data for both terms of the difference is the correct
point-in-time choice, not lookahead. It would only be lookahead if
as_of_date predated when that information became public, which it never
does here.

Composes universe.sp500_universe_breadth() (already point-in-time correct)
unchanged, called twice with the same as_of_date and two different
period_of_report values -- no new PIT logic needed. Both calls also
restrict to sp500_members(as_of_date), the SAME universe set both times
(that function only depends on as_of_date) -- correct, since a name that
has since dropped out of the S&P 500 as of the current formation date
shouldn't be in the active signal at all today, regardless of its
historical 13F breadth.

Known methodological property, disclosed not silently patched: raw_change
is an UNSCALED count difference, and baseline breadth varies enormously
across the S&P 500 (hundreds to 6,000+ filers). Checked empirically against
real data: correlation of ~0.33-0.37 between breadth_prior (a size proxy)
and both |raw_change| and rank-extremity -- the extreme deciles, especially
the "biggest decrease" side, skew toward larger names. This is essentially
guaranteed by counting-statistics variance scaling with baseline level, not
a bug, and the raw (not percentage) formula was an explicit decision from
the original factor-design session -- a percentage-based alternative has
its own worse problem (a low-baseline name going from 5 to 10 holders would
read as a "100% increase," mostly noise). Left as-is; worth surfacing in
the memo's noise-vs-signal discussion, not fixed unilaterally here.
"""

from pathlib import Path

import pandas as pd

from src import universe


def prior_quarter_end(period_of_report) -> pd.Timestamp:
    """The quarter-end immediately preceding period_of_report (13F periods
    are always quarter-end dates). Uses pandas Period arithmetic rather
    than a fixed 3-month offset, to be robust regardless of exact date
    semantics (e.g. leap years, month-length differences)."""
    return (pd.Period(pd.Timestamp(period_of_report), freq="Q") - 1).end_time.normalize()


def breadth_change(
    panel: pd.DataFrame,
    period_of_report,
    as_of_date,
    mapping: pd.DataFrame,
    *,
    passive_ciks: set[str] | None = None,
    history_path: Path = universe.DEFAULT_HISTORY_PATH,
    ticker_to_cusip: pd.Series | None = None,
) -> pd.DataFrame:
    """Un-standardized breadth change: [CUSIP, breadth, breadth_prior, raw_change].

    A CUSIP in the current universe with no recorded breadth for the prior
    quarter (most commonly: newly added to the S&P 500, no 13F history yet)
    is dropped from the result entirely via the inner join below -- not
    imputed. An "infinite increase" from a missing baseline would really
    just encode "was added to the index," not genuine ownership momentum,
    and would contaminate the top of the cross-section with
    index-reconstitution events unrelated to the factor hypothesis.
    """
    current = universe.sp500_universe_breadth(
        panel, period_of_report, as_of_date, mapping,
        passive_ciks=passive_ciks, history_path=history_path, ticker_to_cusip=ticker_to_cusip,
    )
    prior = universe.sp500_universe_breadth(
        panel, prior_quarter_end(period_of_report), as_of_date, mapping,
        passive_ciks=passive_ciks, history_path=history_path, ticker_to_cusip=ticker_to_cusip,
    )
    merged = current.merge(prior, on="CUSIP", how="inner", suffixes=("", "_prior"))
    merged["raw_change"] = merged["breadth"] - merged["breadth_prior"]
    return merged[["CUSIP", "breadth", "breadth_prior", "raw_change"]]


def standardize_cross_section(df: pd.DataFrame, *, column: str = "raw_change") -> pd.DataFrame:
    """Adds rank_signal (percentile rank, primary signal for backtest sorts
    -- bounded, robust to outliers by construction) and zscore_signal
    (secondary robustness-check column, NOT winsorized -- more sensitive to
    fat-tailed raw_change values than rank_signal, disclosed not hidden).

    Guards a degenerate cross-section (std == 0 or NaN, e.g. a single-row
    or constant-value input) by returning NaN for zscore_signal rather than
    raising or producing inf.
    """
    df = df.copy()
    df["rank_signal"] = df[column].rank(pct=True)
    std = df[column].std()
    if std and not pd.isna(std):
        df["zscore_signal"] = (df[column] - df[column].mean()) / std
    else:
        df["zscore_signal"] = float("nan")
    return df


def breadth_momentum(
    panel: pd.DataFrame,
    period_of_report,
    as_of_date,
    mapping: pd.DataFrame,
    *,
    passive_ciks: set[str] | None = None,
    history_path: Path = universe.DEFAULT_HISTORY_PATH,
    ticker_to_cusip: pd.Series | None = None,
) -> pd.DataFrame:
    """Full per-formation-date factor contract: composes breadth_change()
    and standardize_cross_section(). Returns [CUSIP, breadth, breadth_prior,
    raw_change, rank_signal, zscore_signal]."""
    changed = breadth_change(
        panel, period_of_report, as_of_date, mapping,
        passive_ciks=passive_ciks, history_path=history_path, ticker_to_cusip=ticker_to_cusip,
    )
    return standardize_cross_section(changed)
