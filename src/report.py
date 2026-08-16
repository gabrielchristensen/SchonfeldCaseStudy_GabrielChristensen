"""Self-contained HTML backtest report.

Renders src.backtest.run_backtest()'s results dict (optionally combined
with src.detail's regime/attribution derivations) into a single HTML file
with no external assets and no JavaScript -- every chart is a matplotlib
figure embedded directly as a base64 JPEG data URI, matching CLAUDE.md's
"self-contained HTML deliverable" requirement for results/. Each chart is
also written out as a standalone .jpg under a `charts/` subfolder next to
the report, so individual figures can be reused outside the HTML (e.g. in
the technical-defense deck) without re-running anything.

Layout mirrors the case-study prompt's own evaluation criteria: a headline
verdict + KPI row, a primary equity curve and drawdown (underwater) profile
against both benchmarks, then the lag and cost sensitivity grids the prompt
explicitly asks about (rebalancing timing / transaction costs) plus the full
lag x cost grid those two slices
are cut from, then turnover and universe-coverage as supporting
diagnostics, then regime/attribution (per-era performance, benchmark
correlation, top-contributor tickers -- the same analysis
notebooks/regime_and_attribution.ipynb performs, surfaced here so a reader
of the HTML report doesn't have to go find the notebook), then a disclosures
section naming every caveat already surfaced in Phases 1-4, backed by real
measured numbers where the data is available rather than qualitative-only
prose.
"""

import base64
import io
import re
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

from src import detail
from src.backtest import (
    BENCHMARK_TICKER,
    COST_BPS_GRID,
    LAG_DAYS_GRID,
    N_QUANTILES,
    PRIMARY_COST_BPS,
    PRIMARY_LAG_DAYS,
    performance_stats,
)

DEFAULT_OUT_PATH = Path("results/backtest_report.html")
DEFAULT_CHARTS_SUBDIR = "charts"

# --- Palette (validated default from the dataviz skill's six-checks palette;
# see references/palette.md -- categorical slots 1/2/3/8, status good/critical,
# diverging blue<->red pair, neutral chart chrome) ------------------------
COLOR_ACCENT = "#2a78d6"    # slot 1, blue -- primary series / emphasis
COLOR_SLOT2 = "#eb6834"     # slot 2, orange
COLOR_SLOT3 = "#1baf7a"     # slot 3, aqua
COLOR_MUTED = "#a7a59d"     # de-emphasis gray -- benchmarks in emphasis charts
COLOR_GOOD = "#0ca30c"
COLOR_BAD = "#d03b3b"
COLOR_DIVERGING_POS = "#2a78d6"
COLOR_DIVERGING_NEG = "#e34948"
INK = "#0b0b0b"
MUTED_TEXT = "#52514e"
GRID_COLOR = "#e1e0d9"
AXIS_COLOR = "#c3c2b7"

sns.set_theme(context="notebook", style="white")


def _style_ax(ax) -> None:
    ax.set_axisbelow(True)
    ax.grid(axis="y", color=GRID_COLOR, linewidth=0.8)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(AXIS_COLOR)
    ax.tick_params(colors=MUTED_TEXT, labelsize=8)
    ax.title.set_color(INK)
    ax.title.set_fontsize(11)
    ax.title.set_fontweight("bold")
    ax.yaxis.label.set_color(MUTED_TEXT)
    ax.yaxis.label.set_fontsize(9)


_CSS = """
:root {
  color-scheme: light;
  --surface: #fcfcfb; --page: #f9f9f7; --ink: #0b0b0b; --ink-secondary: #52514e;
  --ink-muted: #898781; --border: rgba(11,11,11,0.10); --grid: #e1e0d9;
  --accent: #2a78d6; --good: #006300; --bad: #d03b3b;
  --good-bg: #eaf6ea; --bad-bg: #fbeaea; --warn-bg: #fff8e1; --warn-border: #f0ad4e;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --surface: #232322; --page: #0d0d0d; --ink: #ffffff; --ink-secondary: #c3c2b7;
    --ink-muted: #898781; --border: rgba(255,255,255,0.12); --grid: #2c2c2a;
    --accent: #3987e5; --good: #0ca30c; --bad: #e66767;
    --good-bg: #163016; --bad-bg: #341616; --warn-bg: #332a10; --warn-border: #c98500;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface: #232322; --page: #0d0d0d; --ink: #ffffff; --ink-secondary: #c3c2b7;
  --ink-muted: #898781; --border: rgba(255,255,255,0.12); --grid: #2c2c2a;
  --accent: #3987e5; --good: #0ca30c; --bad: #e66767;
  --good-bg: #163016; --bad-bg: #341616; --warn-bg: #332a10; --warn-border: #c98500;
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  max-width: 1080px; margin: 0 auto; padding: 0 20px 60px; color: var(--ink);
  background: var(--page); line-height: 1.5;
}
h1 { font-size: 1.7em; padding: 28px 0 10px; margin: 0; }
h2 { font-size: 1.2em; margin-top: 2.4em; padding-bottom: 6px; border-bottom: 1px solid var(--border); scroll-margin-top: 70px; }
p, li { color: var(--ink-secondary); }
a { color: var(--accent); }
table { border-collapse: collapse; width: 100%; margin: 1em 0; font-size: 0.9em; font-variant-numeric: tabular-nums; }
th, td { border: 1px solid var(--border); padding: 6px 10px; text-align: right; }
th { background: var(--surface); text-align: center; color: var(--ink-secondary); font-weight: 600; }
td:first-child, th:first-child { text-align: left; }
td.pos { color: var(--good); }
td.neg { color: var(--bad); }
.chart-card { background: #fcfcfb; border: 1px solid var(--border); border-radius: 10px; padding: 14px; margin: 1em 0; }
.chart-card img { max-width: 100%; height: auto; display: block; margin: 0 auto; }
.caveat { background: var(--warn-bg); border-left: 4px solid var(--warn-border); padding: 10px 14px; margin: 1em 0; border-radius: 0 6px 6px 0; }
.meta { color: var(--ink-muted); font-size: 0.9em; }
code { background: var(--surface); padding: 1px 5px; border-radius: 3px; }
.toc { position: sticky; top: 0; background: var(--page); padding: 12px 0; border-bottom: 1px solid var(--border); z-index: 10; margin-bottom: 8px; }
.toc a { margin-right: 16px; font-size: 0.85em; text-decoration: none; color: var(--ink-secondary); white-space: nowrap; }
.toc a:hover { color: var(--accent); }
.toc-row { display: flex; flex-wrap: wrap; gap: 4px 0; overflow-x: auto; }
.kpi-row { display: flex; flex-wrap: wrap; gap: 10px; margin: 14px 0 20px; }
.kpi { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; flex: 1 1 160px; }
.kpi-label { font-size: 0.75em; color: var(--ink-muted); text-transform: uppercase; letter-spacing: 0.03em; }
.kpi-value { font-size: 1.05em; font-weight: 600; margin-top: 2px; }
.verdict { border-radius: 8px; padding: 14px 18px; margin: 1em 0 1.5em; font-size: 1.02em; }
.verdict-good { background: var(--good-bg); border: 1px solid var(--good); }
.verdict-critical { background: var(--bad-bg); border: 1px solid var(--bad); }
"""


def _fig_to_data_uri(fig, *, name: str, charts_dir: Path | None) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="jpg", dpi=160, bbox_inches="tight", pil_kwargs={"quality": 90})
    plt.close(fig)
    raw = buf.getvalue()
    if charts_dir is not None:
        charts_dir.mkdir(parents=True, exist_ok=True)
        (charts_dir / f"{name}.jpg").write_bytes(raw)
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _drawdown(nav: pd.Series) -> pd.Series:
    """Underwater series: fractional decline from the running peak. Same
    formula src.backtest.performance_stats() uses for max_drawdown (just
    the full series instead of its min), so the chart and the headline
    number can never silently diverge."""
    return nav / nav.cummax() - 1


def _line_chart(
    series_dict: dict, *, title: str, ylabel: str, name: str, charts_dir: Path | None,
    colors: dict | None = None, emphasize: str | None = None,
) -> str:
    fig, ax = plt.subplots(figsize=(8.4, 4.2))
    default_colors = [COLOR_ACCENT, COLOR_SLOT2, COLOR_SLOT3]
    colors = colors or {}
    slot = 0
    for label, series in series_dict.items():
        if series is None or series.empty:
            continue
        color = colors.get(label)
        if color is None:
            color = default_colors[slot % len(default_colors)]
            slot += 1
        is_emphasized = emphasize is None or label == emphasize
        ax.plot(
            series.index, series.values, label=label, color=color,
            linewidth=2.0 if is_emphasized else 1.3,
            alpha=1.0 if is_emphasized else 0.6,
        )
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.legend(fontsize=8, frameon=False)
    _style_ax(ax)
    fig.autofmt_xdate()
    return _fig_to_data_uri(fig, name=name, charts_dir=charts_dir)


def _set_thinned_xticks(ax, labels: list[str], *, max_labels: int = 14) -> None:
    """Caps the number of visible x-tick labels on a dense categorical axis
    (e.g. one bar per quarter over an 11-year window) so labels never
    overlap into an unreadable smear -- shows every Nth label, evenly
    spaced, rather than letting matplotlib's default one-per-category
    behavior collide."""
    n = len(labels)
    step = max(1, -(-n // max_labels))  # ceil division
    positions = list(range(0, n, step))
    ax.set_xticks(positions)
    ax.set_xticklabels([labels[i] for i in positions], rotation=45, ha="right", fontsize=7.5)


def _bar_chart(
    labels: list, values: list, *, title: str, ylabel: str, name: str, charts_dir: Path | None,
    color=COLOR_ACCENT, bar_colors: list | None = None,
) -> str:
    width_in = 6.4 if len(labels) <= 14 else min(6.4 + 0.14 * len(labels), 13.0)
    fig, ax = plt.subplots(figsize=(width_in, 3.6))
    x = range(len(labels))
    ax.bar(list(x), values, color=bar_colors if bar_colors is not None else color, width=0.7)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.axhline(0, color=AXIS_COLOR, linewidth=0.8)
    _set_thinned_xticks(ax, [str(l) for l in labels])
    _style_ax(ax)
    return _fig_to_data_uri(fig, name=name, charts_dir=charts_dir)


def _grouped_bar_chart(
    labels: list, series_dict: dict, *, title: str, ylabel: str, name: str, charts_dir: Path | None,
) -> str:
    width_in = 7.2 if len(labels) <= 14 else min(7.2 + 0.14 * len(labels), 13.0)
    fig, ax = plt.subplots(figsize=(width_in, 3.6))
    n = len(series_dict)
    width = 0.8 / max(n, 1)
    x = range(len(labels))
    colors = [COLOR_ACCENT, COLOR_SLOT2, COLOR_SLOT3]
    for i, (label, values) in enumerate(series_dict.items()):
        offset = (i - (n - 1) / 2) * width
        ax.bar([xi + offset for xi in x], values, width=width, label=label, color=colors[i % len(colors)])
    _set_thinned_xticks(ax, [str(l) for l in labels])
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.legend(fontsize=8, frameon=False)
    _style_ax(ax)
    return _fig_to_data_uri(fig, name=name, charts_dir=charts_dir)


_STATS_COLS = ["total_return", "annualized_return", "annualized_vol", "sharpe", "max_drawdown", "hit_rate", "n_days"]


def _cell_class(col: str, value: float) -> str:
    if col in {"total_return", "annualized_return", "sharpe", "max_drawdown"}:
        return "pos" if value > 0 else "neg" if value < 0 else ""
    return ""


def _stats_table(rows: list[dict], *, row_label_key: str, row_label_name: str) -> str:
    if not rows:
        return "<p><em>No closed quarters available.</em></p>"
    header = "".join(f"<th>{c.replace('_', ' ')}</th>" for c in _STATS_COLS)
    body = ""
    for row in rows:
        cells = ""
        for c in _STATS_COLS:
            value = row[c]
            cls = _cell_class(c, value)
            cls_attr = f' class="{cls}"' if cls else ""
            if c in {"total_return", "annualized_return", "annualized_vol", "max_drawdown", "hit_rate"}:
                text = f"{value:.2%}"
            elif c == "sharpe":
                text = f"{value:.2f}"
            elif pd.isna(value):
                # performance_stats() deliberately returns NaN for every
                # stat (n_days included) when a series has fewer than 2
                # NAV points -- a real, degenerate-but-valid case (e.g. a
                # window with zero closed quarters), not a formatting
                # afterthought. int(nan) raises ValueError; caught for
                # real by running --mode smoke against a tiny sample
                # window that happened to close zero quarters.
                text = "n/a"
            else:
                text = f"{int(value)}"
            cells += f"<td{cls_attr}>{text}</td>"
        body += f"<tr><td>{row[row_label_key]}</td>{cells}</tr>"
    return f"<table><tr><th>{row_label_name}</th>{header}</tr>{body}</table>"


def _diverging_bg(value: float, *, vmax: float) -> str:
    if vmax <= 0 or pd.isna(value):
        return "transparent"
    t = max(-1.0, min(1.0, value / vmax))
    base = (0x2A, 0x78, 0xD6) if t >= 0 else (0xE3, 0x49, 0x48)
    alpha = 0.10 + 0.45 * abs(t)
    return f"rgba({base[0]},{base[1]},{base[2]},{alpha:.2f})"


def _grid_heatmap_table(results: dict, *, lag_days_grid=LAG_DAYS_GRID, cost_bps_grid=COST_BPS_GRID) -> str:
    cells = {}
    for lag in lag_days_grid:
        for cost in cost_bps_grid:
            key = (lag, cost)
            if key in results:
                cells[key] = performance_stats(results[key]["spread_nav"])["sharpe"]
    if not cells:
        return "<p><em>No grid results available.</em></p>"
    vmax = max(abs(v) for v in cells.values()) or 1.0
    header = "".join(f"<th>{c}bps</th>" for c in cost_bps_grid)
    rows_html = ""
    for lag in lag_days_grid:
        row_cells = ""
        for cost in cost_bps_grid:
            v = cells.get((lag, cost))
            if v is None:
                row_cells += "<td>-</td>"
            else:
                bg = _diverging_bg(v, vmax=vmax)
                row_cells += f'<td style="background:{bg}">{v:.2f}</td>'
        rows_html += f"<tr><td>{lag}d</td>{row_cells}</tr>"
    return f'<table><tr><th>Lag \\ Cost</th>{header}</tr>{rows_html}</table>'


def _kpi_tiles(meta: dict) -> str:
    tiles = []
    if meta.get("window"):
        tiles.append(("Backtest window", meta["window"]))
    tiles.append(("Formation lag", f"{PRIMARY_LAG_DAYS}d primary ({'/'.join(str(l) for l in LAG_DAYS_GRID)}d swept)"))
    tiles.append(("Transaction cost", f"{PRIMARY_COST_BPS}bps primary ({'/'.join(str(c) for c in COST_BPS_GRID)}bps swept)"))
    tiles.append(("Portfolio", f"{N_QUANTILES} deciles, top vs. bottom, equal-weight"))
    tiles.append(("Benchmarks", f"{BENCHMARK_TICKER} + internal equal-weight universe"))
    if meta.get("price universe (tickers)"):
        tiles.append(("Price universe", f"{meta['price universe (tickers)']} tickers"))
    html = "".join(
        f'<div class="kpi"><div class="kpi-label">{k}</div><div class="kpi-value">{v}</div></div>'
        for k, v in tiles
    )
    return f'<div class="kpi-row">{html}</div>'


def _verdict_callout(spread_stats: dict, universe_stats: dict, spy_stats: dict) -> str:
    spread_sharpe, universe_sharpe, spy_sharpe = spread_stats["sharpe"], universe_stats["sharpe"], spy_stats["sharpe"]
    beats_spy = spread_sharpe > spy_sharpe
    beats_universe = spread_sharpe > universe_sharpe
    status = "good" if (beats_spy and beats_universe) else "critical"
    verdict_text = (
        f"Long-short spread Sharpe <strong>{spread_sharpe:.2f}</strong> "
        f"{'beats' if beats_spy else 'trails'} {BENCHMARK_TICKER} "
        f"(<strong>{spy_sharpe:.2f}</strong>) and "
        f"{'beats' if beats_universe else 'trails'} the internal equal-weight universe "
        f"(<strong>{universe_sharpe:.2f}</strong>). Max drawdown "
        f"<strong>{spread_stats['max_drawdown']:.1%}</strong> vs. {BENCHMARK_TICKER}'s "
        f"<strong>{spy_stats['max_drawdown']:.1%}</strong>."
    )
    return f'<div class="verdict verdict-{status}"><strong>Headline:</strong> {verdict_text}</div>'


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def build_report(
    results: dict,
    out_path: Path = DEFAULT_OUT_PATH,
    *,
    meta: dict | None = None,
    prices: pd.DataFrame | None = None,
    charts_dir: Path | None = None,
) -> Path:
    """Renders `results` (src.backtest.run_backtest()'s output) to a
    self-contained HTML file at `out_path`. `meta` is an optional dict of
    free-form run metadata (window dates, universe size, panel row counts)
    shown in the KPI row. `prices` is the optional price panel
    (`data/processed/prices.parquet`'s schema: ticker/date/adj_close) --
    when supplied (and the primary lag's quarters carry `long_tickers`/
    `short_tickers`, true for any real backtest run), a Regime &
    attribution section is added via src.detail's already-tested,
    committed-artifact-only functions; when omitted, that section is
    skipped entirely. Every chart is written as a standalone .jpg under
    `charts_dir` (default: `out_path.parent / "charts"`) in addition to
    being embedded inline.
    """
    meta = meta or {}
    out_path = Path(out_path)
    charts_dir = Path(charts_dir) if charts_dir is not None else out_path.parent / DEFAULT_CHARTS_SUBDIR
    if charts_dir.is_dir():
        # Clear stale .jpg files from a previous run (e.g. a since-removed
        # section's chart) so charts_dir never accumulates orphans that no
        # longer correspond to anything in the current report.
        for stale in charts_dir.glob("*.jpg"):
            stale.unlink()
    primary_key = (PRIMARY_LAG_DAYS, PRIMARY_COST_BPS)

    # Each entry: (anchor_id_or_None, html). anchor_id is set only for <h2> headings, for the TOC.
    sections: list[tuple[str | None, str]] = []

    def add_heading(text: str) -> str:
        anchor = _slug(text)
        sections.append((anchor, f'<h2 id="{anchor}">{text}</h2>'))
        return anchor

    def add(html: str) -> None:
        sections.append((None, html))

    add(f"<h1>13F Ownership-Breadth-Momentum Backtest</h1>")
    add(_kpi_tiles(meta))

    # --- Primary result -----------------------------------------------
    if primary_key in results:
        primary = results[primary_key]
        spread_stats = performance_stats(primary["spread_nav"])
        universe_stats = performance_stats(primary["universe_nav"])
        spy_stats = performance_stats(primary["spy_nav"])
        add(_verdict_callout(spread_stats, universe_stats, spy_stats))

        add_heading("Primary result")
        chart = _line_chart(
            {
                f"Long-short spread ({PRIMARY_LAG_DAYS}d lag, {PRIMARY_COST_BPS}bps)": primary["spread_nav"],
                "Internal equal-weight universe": primary["universe_nav"],
                BENCHMARK_TICKER: primary["spy_nav"],
            },
            title="Primary backtest: long-short spread vs. benchmarks",
            ylabel="NAV (start = 1.0)",
            name="primary_equity_curve",
            charts_dir=charts_dir,
            colors={"Internal equal-weight universe": COLOR_SLOT2, BENCHMARK_TICKER: COLOR_SLOT3},
            emphasize=f"Long-short spread ({PRIMARY_LAG_DAYS}d lag, {PRIMARY_COST_BPS}bps)",
        )
        add(f'<div class="chart-card"><img src="{chart}" alt="primary equity curve"></div>')

        drawdown_chart = _line_chart(
            {
                f"Long-short spread ({PRIMARY_LAG_DAYS}d lag, {PRIMARY_COST_BPS}bps)": _drawdown(primary["spread_nav"]),
                "Internal equal-weight universe": _drawdown(primary["universe_nav"]),
                BENCHMARK_TICKER: _drawdown(primary["spy_nav"]),
            },
            title="Primary backtest: drawdown (underwater) vs. benchmarks",
            ylabel="Drawdown from running peak",
            name="primary_drawdown",
            charts_dir=charts_dir,
            colors={"Internal equal-weight universe": COLOR_SLOT2, BENCHMARK_TICKER: COLOR_SLOT3},
            emphasize=f"Long-short spread ({PRIMARY_LAG_DAYS}d lag, {PRIMARY_COST_BPS}bps)",
        )
        add(f'<div class="chart-card"><img src="{drawdown_chart}" alt="primary drawdown"></div>')

        stats_rows = [
            {"label": "Long-short spread", **spread_stats},
            {"label": "Internal equal-weight universe", **universe_stats},
            {"label": BENCHMARK_TICKER, **spy_stats},
        ]
        add(_stats_table(stats_rows, row_label_key="label", row_label_name="Series"))
        if primary["open_quarters"]:
            oq = primary["open_quarters"][0]
            add(
                f'<p class="meta">The most recent formation quarter '
                f'({oq["period_of_report"].date()}, formed {oq["as_of_date"].date()}) is still open '
                f"as of this run and is excluded from the stats above -- its next rebalance hasn't happened yet.</p>"
            )

    # --- Lag sensitivity -------------------------------------------------
    lag_series = {
        f"{lag}d lag": results[(lag, PRIMARY_COST_BPS)]["spread_nav"]
        for lag in LAG_DAYS_GRID if (lag, PRIMARY_COST_BPS) in results
    }
    if lag_series:
        add_heading("Formation-lag sensitivity")
        chart = _line_chart(
            lag_series, title=f"Lag sensitivity (cost = {PRIMARY_COST_BPS}bps)", ylabel="NAV (start = 1.0)",
            name="lag_sensitivity", charts_dir=charts_dir,
        )
        add(f'<div class="chart-card"><img src="{chart}" alt="lag sensitivity"></div>')
        lag_rows = [
            {"label": f"{lag}d", **performance_stats(results[(lag, PRIMARY_COST_BPS)]["spread_nav"])}
            for lag in LAG_DAYS_GRID if (lag, PRIMARY_COST_BPS) in results
        ]
        add(_stats_table(lag_rows, row_label_key="label", row_label_name="Lag"))
        add(
            '<div class="caveat"><strong>Read with caution:</strong> a longer lag captures more filers '
            "before formation (96.3% by day 60 vs. 87.0% by day 45, measured on the full panel) but also "
            "enters later into any front-loaded post-disclosure return decay. Whichever direction this table "
            "shows should not be over-read as proof of the underlying mechanism -- it's one realization over "
            "one historical window, not a controlled experiment.</div>"
        )

    # --- Cost sensitivity --------------------------------------------------
    cost_rows_data = []
    for cost_bps in COST_BPS_GRID:
        key = (PRIMARY_LAG_DAYS, cost_bps)
        if key in results:
            cost_rows_data.append((cost_bps, performance_stats(results[key]["spread_nav"])))
    if cost_rows_data:
        add_heading("Transaction-cost sensitivity")
        chart = _bar_chart(
            [f"{c}bps" for c, _ in cost_rows_data], [s["annualized_return"] for _, s in cost_rows_data],
            title=f"Transaction-cost sensitivity ({PRIMARY_LAG_DAYS}d lag)", ylabel="Annualized return",
            name="cost_sensitivity", charts_dir=charts_dir,
        )
        add(f'<div class="chart-card"><img src="{chart}" alt="cost sensitivity"></div>')
        cost_rows = [{"label": f"{c}bps", **s} for c, s in cost_rows_data]
        add(_stats_table(cost_rows, row_label_key="label", row_label_name="Cost"))

    # --- Full lag x cost grid ------------------------------------------
    if any(k in results for k in ((l, c) for l in LAG_DAYS_GRID for c in COST_BPS_GRID)):
        add_heading("Full lag x cost grid (Sharpe)")
        add(
            '<p class="meta">Every combination the sweep actually computes, not just the two 1-D slices '
            "above -- darker blue is a higher Sharpe, darker red is lower/negative.</p>"
        )
        add(_grid_heatmap_table(results))

    # --- Turnover --------------------------------------------------------
    if primary_key in results and results[primary_key]["quarters"]:
        quarters = results[primary_key]["quarters"]
        labels = [q["period_of_report"].date().isoformat() for q in quarters]
        add_heading("Turnover")
        chart = _grouped_bar_chart(
            labels,
            {"Long leg": [q["turnover_long"] for q in quarters], "Short leg": [q.get("turnover_short", float("nan")) for q in quarters]},
            title="Turnover by quarter", ylabel="One-way turnover fraction",
            name="turnover", charts_dir=charts_dir,
        )
        add(f'<div class="chart-card"><img src="{chart}" alt="turnover"></div>')

    # --- Universe coverage ------------------------------------------------
    if primary_key in results and results[primary_key]["quarters"] and all("n_names" in q for q in results[primary_key]["quarters"]):
        quarters = results[primary_key]["quarters"]
        periods = [q["period_of_report"] for q in quarters]
        n_names = pd.Series([q["n_names"] for q in quarters], index=periods)
        dropped_counts = pd.Series([len(q.get("dropped", ())) for q in quarters], index=periods)
        add_heading("Universe coverage")
        chart1 = _line_chart(
            {"Scored cross-section size": n_names}, title="Names scored per quarter", ylabel="n_names",
            name="coverage_n_names", charts_dir=charts_dir,
        )
        chart2 = _bar_chart(
            [p.date().isoformat() for p in periods], dropped_counts.tolist(),
            title="Tickers dropped for missing price data", ylabel="Dropped count",
            name="coverage_dropped", charts_dir=charts_dir, color=COLOR_SLOT2,
        )
        add(
            f'<div class="chart-card"><img src="{chart1}" alt="universe size"></div>'
            f'<div class="chart-card"><img src="{chart2}" alt="dropped tickers"></div>'
        )

    # --- Regime & attribution (requires prices; reuses src.detail) --------
    primary_quarters = results.get(primary_key, {}).get("quarters", [])
    can_do_regime = (
        prices is not None and primary_key in results and primary_quarters
        and all("long_tickers" in q and "short_tickers" in q for q in primary_quarters)
    )
    if can_do_regime:
        add_heading("Regime & attribution")
        add(
            '<p class="meta">Splits the primary lag\'s closed quarters into contiguous, roughly-equal '
            "sub-periods and re-runs the same performance/IC stats on each -- and attributes the full-window "
            "spread return to individual tickers. Reuses src.detail's already-tested functions against the "
            "committed backtest_results.pkl + prices.parquet, no re-run of the backtest.</p>"
        )

        subperiod_df = detail.subperiod_stats(results, PRIMARY_LAG_DAYS, cost_bps=PRIMARY_COST_BPS, n_splits=2)
        subperiod_navs_list = detail.subperiod_navs(results, PRIMARY_LAG_DAYS, cost_bps=PRIMARY_COST_BPS, n_splits=2)
        if not subperiod_df.empty:
            nav_series = {}
            for _, row in subperiod_df.iterrows():
                chunk_label = f"{row['start_period'].date()} - {row['end_period'].date()}"
                if row["chunk"] < len(subperiod_navs_list):
                    nav_series[chunk_label] = subperiod_navs_list[row["chunk"]]
            chart = _line_chart(
                nav_series, title="Spread NAV by regime (sub-period)", ylabel="NAV (start = 1.0 per chunk)",
                name="regime_equity", charts_dir=charts_dir,
            )
            add(f'<div class="chart-card"><img src="{chart}" alt="regime equity curves"></div>')
            rows = [
                {"label": f"{row['start_period'].date()} - {row['end_period'].date()}", **{k: row[k] for k in _STATS_COLS}}
                for _, row in subperiod_df.iterrows()
            ]
            add(_stats_table(rows, row_label_key="label", row_label_name="Sub-period"))
            ic_bits = "; ".join(
                f"{row['start_period'].date()}-{row['end_period'].date()}: mean IC {row['mean_ic']:.3f} "
                f"({row['n_ic_quarters']} quarters)"
                for _, row in subperiod_df.iterrows()
            )
            add(f"<p>Rank-IC by regime: {ic_bits}.</p>")

        corr = detail.benchmark_correlation(results, PRIMARY_LAG_DAYS, cost_bps=PRIMARY_COST_BPS)
        add("<h3>Benchmark correlation</h3>")
        corr_df = corr["correlation"]
        corr_header = "".join(f"<th>{c}</th>" for c in corr_df.columns)
        corr_body = "".join(
            "<tr><td>" + idx + "</td>" + "".join(f"<td>{corr_df.loc[idx, c]:.2f}</td>" for c in corr_df.columns) + "</tr>"
            for idx in corr_df.index
        )
        add(f"<table><tr><th>Daily-return corr</th>{corr_header}</tr>{corr_body}</table>")
        add(
            f'<p class="meta">Beta vs. {BENCHMARK_TICKER}: {corr["beta_vs_spy"]:.2f}. '
            f'Beta vs. internal universe: {corr["beta_vs_universe"]:.2f}.</p>'
        )

        try:
            detail_df = detail.quarter_asset_detail(results, prices, lag_days_grid=[PRIMARY_LAG_DAYS], cost_bps=PRIMARY_COST_BPS)
        except ValueError:
            detail_df = pd.DataFrame()
        if not detail_df.empty:
            contrib = detail_df.groupby("ticker")["contribution"].sum()
            top = contrib.reindex(contrib.abs().sort_values(ascending=False).index[:10])
            bar_colors = [COLOR_GOOD if v > 0 else COLOR_BAD for v in top.values]
            chart = _bar_chart(
                list(top.index), top.tolist(), title="Top-10 tickers by |contribution| to spread return",
                ylabel="Summed contribution", name="regime_top_contributors", charts_dir=charts_dir,
                bar_colors=bar_colors,
            )
            add("<h3>Attribution</h3>")
            add(f'<div class="chart-card"><img src="{chart}" alt="top contributors"></div>')

    # --- Disclosures ------------------------------------------------------
    add_heading("Known limitations, disclosed")
    coverage_bullet = (
        "<li><strong>Missing price data:</strong> a name with no usable adjusted-close data for a given "
        "quarter is dropped from that quarter's basket (not imputed) -- see the panel's dropped-ticker log.</li>"
    )
    if primary_key in results and results[primary_key]["quarters"] and all("dropped" in q for q in results[primary_key]["quarters"]):
        quarters = results[primary_key]["quarters"]
        total_dropped = sum(len(q["dropped"]) for q in quarters)
        quarters_with_drops = sum(1 for q in quarters if q["dropped"])
        coverage_bullet = (
            f"<li><strong>Missing price data:</strong> a name with no usable adjusted-close data for a given "
            f"quarter is dropped from that quarter's basket (not imputed) -- {total_dropped} ticker-quarter "
            f"drop(s) across {quarters_with_drops} of {len(quarters)} closed quarters at the primary "
            f"{PRIMARY_LAG_DAYS}d lag.</li>"
        )
    add(
        '<div class="caveat">'
        "<ul>"
        "<li><strong>CUSIP-mapping coverage gap:</strong> only tickers with a resolved CUSIP crosswalk "
        "entry (OpenFIGI, current-ticker-only) are tradable here -- names acquired, merged, or delisted "
        "mid-window are systematically under-represented; the internal equal-weight universe benchmark "
        "exists specifically to separate this effect from genuine factor skill.</li>"
        "<li><strong>Mega-cap tilt:</strong> raw (unscaled) breadth change mechanically favors names with "
        "a larger baseline breadth, most visible in the short leg -- documented in Phase 3, not corrected.</li>"
        f"{coverage_bullet}"
        "<li><strong>rf = 0</strong> for all Sharpe ratios -- no risk-free-rate series was sourced.</li>"
        "<li><strong>SPY / internal benchmark carry no simulated transaction costs</strong>; only the "
        "long-short strategy legs do.</li>"
        "</ul></div>"
    )

    toc_links = "".join(f'<a href="#{anchor}">{re.sub("<[^>]+>", "", html)}</a>' for anchor, html in sections if anchor)
    toc = f'<nav class="toc"><div class="toc-row">{toc_links}</div></nav>'

    body = "\n".join(html for _, html in sections)
    html = f"<title>13F Backtest Report</title>\n<meta charset=\"utf-8\">\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<style>{_CSS}</style>\n{toc}\n{body}\n"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    return out_path


def main() -> None:
    import argparse
    import pickle

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="data/processed/backtest_results.pkl")
    parser.add_argument("--prices", default="data/processed/prices.parquet")
    parser.add_argument("--out", default=str(DEFAULT_OUT_PATH))
    args = parser.parse_args()

    with open(args.results, "rb") as fh:
        payload = pickle.load(fh)
    results = payload["results"] if isinstance(payload, dict) and "results" in payload else payload
    meta = payload.get("meta") if isinstance(payload, dict) else None

    prices = None
    prices_path = Path(args.prices)
    if prices_path.exists():
        prices = pd.read_parquet(prices_path)
    else:
        print(f"Note: {prices_path} not found -- skipping the Regime & attribution section.")

    out_path = build_report(results, out_path=Path(args.out), meta=meta, prices=prices)
    print(f"Report written -> {out_path}")


if __name__ == "__main__":
    main()
