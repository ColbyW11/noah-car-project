"""Streamlit dashboard for VW oil-change availability data.

Three lenses on the same dataset: availability (what customers see),
scraper health (is the pipeline working?), and per-dealer comparison
(the "fastest oil change near me" view from SPEC.md).

    uv run streamlit run scripts/dashboard.py

Reads from:
- `data/processed/timeseries.parquet`  — successful observations (KPIs, trends)
- `data/raw/<date>/observations.jsonl` — raw slot lists + error observations
- `data/raw/<date>/run_metadata.json`  — per-run duration + success counts
- `data/dealer_master.csv`             — dealer names / platform / region for joins

All loaders are cached on file mtime so re-renders are cheap and a fresh
daily run invalidates the cache automatically.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import plotly.express as px
import plotly.graph_objects as go
import polars as pl
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent.parent
PARQUET_PATH = REPO_ROOT / "data" / "processed" / "timeseries.parquet"
RAW_DIR = REPO_ROOT / "data" / "raw"
REGISTRY_CSV = REPO_ROOT / "data" / "dealer_master.csv"

ERROR_PREFIXES = ("TIMEOUT:", "PARSE:", "NAVIGATION:", "UNEXPECTED:")


# ---------------------------------------------------------------------------
# Loaders (cached on file mtime so re-renders are free; fresh runs invalidate)
# ---------------------------------------------------------------------------


def _mtime(path: Path) -> float:
    return path.stat().st_mtime if path.exists() else 0.0


@st.cache_data(show_spinner=False)
def load_timeseries(_mtime_key: float) -> pl.DataFrame:
    """Successful per-observation rows from the master parquet."""
    if not PARQUET_PATH.exists():
        return pl.DataFrame()
    return pl.read_parquet(PARQUET_PATH)


@st.cache_data(show_spinner=False)
def load_registry(_mtime_key: float) -> pl.DataFrame:
    if not REGISTRY_CSV.exists():
        return pl.DataFrame()
    return pl.read_csv(REGISTRY_CSV).select(
        "dealer_code", "dealer_name", "platform", "region", "active"
    )


@st.cache_data(show_spinner=False)
def load_raw_observations(_mtime_key: tuple[float, ...]) -> pl.DataFrame:
    """Every observation (success + error) flattened from raw JSONL.

    Used for the health tab (which needs error rows) and the slot-detail
    aggregations (which need the full `available_slots` list).
    """
    rows: list[dict] = []
    for jsonl_path in sorted(RAW_DIR.glob("*/observations.jsonl")):
        partition_date = _parse_partition_date(jsonl_path)
        if partition_date is None:
            continue
        for line in jsonl_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obs = json.loads(line)
            except json.JSONDecodeError:
                continue
            obs["observation_date"] = partition_date
            rows.append(obs)
    if not rows:
        return pl.DataFrame()
    return pl.from_dicts(rows, infer_schema_length=None)


@st.cache_data(show_spinner=False)
def load_run_metadata(_mtime_key: tuple[float, ...]) -> pl.DataFrame:
    rows: list[dict] = []
    for meta_path in sorted(RAW_DIR.glob("*/run_metadata.json")):
        try:
            rows.append(json.loads(meta_path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    if not rows:
        return pl.DataFrame()
    return pl.from_dicts(rows, infer_schema_length=None).with_columns(
        pl.col("observation_date").str.to_date()
    )


def _parse_partition_date(jsonl_path: Path) -> date | None:
    try:
        return date.fromisoformat(jsonl_path.parent.name)
    except ValueError:
        return None


def _build_slot_heatmap(raw_df: pl.DataFrame) -> tuple[list[list[int]], int]:
    grid = [[0] * 24 for _ in range(7)]
    total = 0
    if raw_df.is_empty() or "available_slots" not in raw_df.columns:
        return grid, total
    for row in raw_df.iter_rows(named=True):
        if row.get("scrape_status") != "success":
            continue
        for slot_iso in row.get("available_slots") or []:
            try:
                slot = datetime.fromisoformat(slot_iso)
            except (TypeError, ValueError):
                continue
            grid[slot.weekday()][slot.hour] += 1
            total += 1
    return grid, total


def _build_future_density(raw_df: pl.DataFrame) -> pl.DataFrame:
    if raw_df.is_empty() or "available_slots" not in raw_df.columns:
        return pl.DataFrame()
    rows: list[dict] = []
    for row in raw_df.iter_rows(named=True):
        if row.get("scrape_status") != "success":
            continue
        dealer = row["dealer_code"]
        for slot_iso in row.get("available_slots") or []:
            try:
                slot = datetime.fromisoformat(slot_iso)
            except (TypeError, ValueError):
                continue
            rows.append({"dealer_code": dealer, "slot_date": slot.date()})
    if not rows:
        return pl.DataFrame()
    return (
        pl.from_dicts(rows)
        .group_by(["slot_date", "dealer_code"])
        .agg(pl.len().alias("slots"))
        .sort(["slot_date", "dealer_code"])
    )


def _raw_mtime_key() -> tuple[float, ...]:
    """Tuple of mtimes for every raw JSONL — cache busts on any change."""
    return tuple(
        _mtime(p) for p in sorted(RAW_DIR.glob("*/observations.jsonl"))
    )


def _run_meta_mtime_key() -> tuple[float, ...]:
    return tuple(
        _mtime(p) for p in sorted(RAW_DIR.glob("*/run_metadata.json"))
    )


# ---------------------------------------------------------------------------
# Page setup + sidebar filters
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="VW oil-change availability",
    page_icon="🚗",
    layout="wide",
)

st.title("VW oil-change availability")

ts = load_timeseries(_mtime(PARQUET_PATH))
raw = load_raw_observations(_raw_mtime_key())
registry = load_registry(_mtime(REGISTRY_CSV))
run_meta = load_run_metadata(_run_meta_mtime_key())

if ts.is_empty() and raw.is_empty():
    st.warning(
        "No data yet. Run `uv run python scripts/run_daily.py` to "
        "produce the first batch of observations."
    )
    st.stop()

# Latest observation across all sources
latest_ts: datetime | None = None
if not ts.is_empty():
    latest_ts = ts["observation_ts"].max()  # type: ignore[assignment]
elif not raw.is_empty() and "observation_ts" in raw.columns:
    parsed = raw["observation_ts"].str.to_datetime(strict=False)
    latest_ts = parsed.max()  # type: ignore[assignment]
if latest_ts is not None:
    st.caption(f"Data through **{latest_ts:%Y-%m-%d %H:%M UTC}**")

# Date range bounds
all_dates: list[date] = []
if not ts.is_empty():
    all_dates.extend(ts["observation_date"].to_list())
if not raw.is_empty():
    all_dates.extend(raw["observation_date"].to_list())
min_date = min(all_dates) if all_dates else date.today() - timedelta(days=7)
max_date = max(all_dates) if all_dates else date.today()
default_start = max(min_date, max_date - timedelta(days=6))

st.sidebar.header("Filters")
date_range = st.sidebar.date_input(
    "Date range",
    value=(default_start, max_date),
    min_value=min_date,
    max_value=max_date,
)
if isinstance(date_range, tuple) and len(date_range) == 2:
    start_date, end_date = date_range
else:
    start_date = end_date = max_date  # user mid-selection

# Dealer multi-select. Default to dealers that appear in either timeseries
# or raw data so a fully-erroring dealer (e.g. VW0005 on 2026-05-15) is
# still visible.
dealer_codes = sorted(
    set(ts["dealer_code"].to_list() if not ts.is_empty() else [])
    | set(raw["dealer_code"].to_list() if not raw.is_empty() else [])
)
dealer_labels: dict[str, str] = {}
if not registry.is_empty():
    for row in registry.iter_rows(named=True):
        dealer_labels[row["dealer_code"]] = (
            f"{row['dealer_code']} — {row['dealer_name']}"
        )
selected_dealers = st.sidebar.multiselect(
    "Dealers",
    options=dealer_codes,
    default=dealer_codes,
    format_func=lambda c: dealer_labels.get(c, c),
)
include_errors = st.sidebar.toggle(
    "Include error observations", value=True,
    help="Errors only appear on the Scraper health tab regardless — this "
    "toggle controls whether they count toward the success-rate KPI.",
)


# ---------------------------------------------------------------------------
# Apply filters
# ---------------------------------------------------------------------------


def _filter_window(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty():
        return df
    out = df.filter(
        (pl.col("observation_date") >= start_date)
        & (pl.col("observation_date") <= end_date)
    )
    if selected_dealers:
        out = out.filter(pl.col("dealer_code").is_in(selected_dealers))
    return out


ts_window = _filter_window(ts)
raw_window = _filter_window(raw)

# ---------------------------------------------------------------------------
# Header KPIs
# ---------------------------------------------------------------------------

successes_in_window = ts_window.height
errors_in_window = (
    raw_window.filter(pl.col("scrape_status") == "error").height
    if not raw_window.is_empty() and "scrape_status" in raw_window.columns
    else 0
)
attempts_in_window = successes_in_window + errors_in_window

avg_lead = (
    float(ts_window["lead_time_hours"].mean() or 0.0)
    if successes_in_window
    else 0.0
)
next_day_pct = (
    100 * ts_window.filter(pl.col("lead_time_hours") <= 48).height
    / successes_in_window
    if successes_in_window
    else 0.0
)
success_pct = (
    100 * successes_in_window / attempts_in_window if attempts_in_window else 0.0
)
total_slots = (
    int(ts_window["slot_count"].sum() or 0) if successes_in_window else 0
)

k1, k2, k3, k4 = st.columns(4)
k1.metric("Avg lead time", f"{avg_lead:.1f} h")
k2.metric("Next-day rate", f"{next_day_pct:.0f}%", help="% of obs with first slot ≤ 48h")
k3.metric("Success rate", f"{success_pct:.0f}%", help=f"{successes_in_window}/{attempts_in_window} attempts")
k4.metric("Slots observed", f"{total_slots:,}")

if successes_in_window == 0:
    st.info(
        "No successful observations in the selected window — try widening "
        "the date range or check the Scraper health tab."
    )

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_avail, tab_health, tab_dealer = st.tabs(
    ["Availability", "Scraper health", "Per-dealer comparison"]
)


# ---- Tab 1: Availability ----------------------------------------------------

with tab_avail:
    if successes_in_window == 0:
        st.caption("Nothing to plot without successful observations.")
    else:
        st.subheader("Lead time over observations")
        line_df = ts_window.sort("observation_ts").to_pandas()
        fig_lead = px.line(
            line_df,
            x="observation_ts",
            y="lead_time_hours",
            color="dealer_code",
            markers=True,
            labels={
                "observation_ts": "Observation",
                "lead_time_hours": "Hours to first slot",
                "dealer_code": "Dealer",
            },
        )
        fig_lead.update_layout(hovermode="x unified")
        st.plotly_chart(fig_lead, width="stretch")

        st.subheader("When slots are available (day × hour)")
        slot_grid, total_slot_obs = _build_slot_heatmap(raw_window)
        if total_slot_obs == 0:
            st.caption("No raw slot data in the selected window.")
        else:
            fig_heat = px.imshow(
                slot_grid,
                x=[f"{h:02d}" for h in range(24)],
                y=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                color_continuous_scale="Viridis",
                aspect="auto",
                labels={"x": "Hour of day (dealer-local)", "y": "Day of week", "color": "Slot count"},
            )
            st.plotly_chart(fig_heat, width="stretch")
            st.caption(f"{total_slot_obs:,} slot observations across the window.")

        st.subheader("Slots offered per future date")
        future = _build_future_density(raw_window)
        if future.is_empty():
            st.caption("No slots offered in the selected window.")
        else:
            fig_future = px.bar(
                future.to_pandas(),
                x="slot_date",
                y="slots",
                color="dealer_code",
                labels={"slot_date": "Slot date", "slots": "Slots offered", "dealer_code": "Dealer"},
            )
            st.plotly_chart(fig_future, width="stretch")


# ---- Tab 2: Scraper health --------------------------------------------------

with tab_health:
    if raw_window.is_empty():
        st.caption("No raw observations in the selected window.")
    else:
        st.subheader("Per-dealer × per-day status")
        status = (
            raw_window.select("dealer_code", "observation_date", "scrape_status")
            .group_by(["dealer_code", "observation_date"])
            .agg(pl.col("scrape_status").first())
        )
        # status_code: 1 = success, 0 = error, null = no attempt
        status = status.with_columns(
            pl.when(pl.col("scrape_status") == "success").then(1)
            .when(pl.col("scrape_status") == "error").then(0)
            .otherwise(None)
            .alias("status_code")
        )
        pivot = status.pivot(
            on="observation_date", index="dealer_code", values="status_code"
        ).sort("dealer_code")
        date_cols = [c for c in pivot.columns if c != "dealer_code"]
        z = [pivot.select(c).to_series().to_list() for c in date_cols]
        # transpose to dealer × date
        z_t = list(map(list, zip(*z))) if z else []
        fig_status = go.Figure(
            data=go.Heatmap(
                z=z_t,
                x=date_cols,
                y=pivot["dealer_code"].to_list(),
                colorscale=[[0.0, "#dc2626"], [1.0, "#16a34a"]],
                zmin=0,
                zmax=1,
                showscale=False,
                xgap=2,
                ygap=2,
                hovertemplate="Dealer %{y} on %{x}: %{z}<extra></extra>",
            )
        )
        fig_status.update_layout(
            xaxis_title="Date",
            yaxis_title="Dealer",
            height=80 + 35 * pivot.height,
        )
        st.plotly_chart(fig_status, width="stretch")
        st.caption("Green = success, red = error, blank = no attempt that day.")

        st.subheader("Error breakdown")
        errors = raw_window.filter(pl.col("scrape_status") == "error")
        if errors.is_empty():
            st.success("No errors in the selected window.")
        else:
            prefix_counts: Counter[str] = Counter()
            for msg in errors["error_message"].to_list():
                if not isinstance(msg, str):
                    continue
                bucket = next(
                    (p.rstrip(":") for p in ERROR_PREFIXES if msg.startswith(p)),
                    "OTHER",
                )
                prefix_counts[bucket] += 1
            err_df = pl.DataFrame(
                {
                    "category": list(prefix_counts.keys()),
                    "count": list(prefix_counts.values()),
                }
            ).sort("count", descending=True)
            fig_err = px.bar(
                err_df.to_pandas(),
                x="category", y="count",
                labels={"category": "Error category", "count": "Errors"},
                color="category",
            )
            st.plotly_chart(fig_err, width="stretch")
            with st.expander(f"{errors.height} error message(s)"):
                st.dataframe(
                    errors.select(
                        "observation_date", "dealer_code", "platform",
                        "error_message"
                    ).sort("observation_date", descending=True).to_pandas(),
                    hide_index=True,
                    width="stretch",
                )

        st.subheader("Scheduling flow time (friction) per dealer")
        flow_rows = ts_window.filter(pl.col("scheduling_flow_seconds").is_not_null())
        if flow_rows.is_empty():
            st.caption("No flow-time data in the selected window.")
        else:
            fig_flow = px.box(
                flow_rows.to_pandas(),
                x="dealer_code", y="scheduling_flow_seconds",
                color="dealer_code", points="all",
                labels={
                    "dealer_code": "Dealer",
                    "scheduling_flow_seconds": "Flow time (s)",
                },
            )
            st.plotly_chart(fig_flow, width="stretch")

    # Run duration trend (from run_metadata.json, not filtered by dealer)
    if not run_meta.is_empty():
        st.subheader("Run duration over time")
        run_window = run_meta.filter(
            (pl.col("observation_date") >= start_date)
            & (pl.col("observation_date") <= end_date)
        ).sort("observation_date")
        if run_window.is_empty():
            st.caption("No runs in the selected window.")
        else:
            fig_runs = px.bar(
                run_window.to_pandas(),
                x="observation_date", y="duration_seconds",
                hover_data=["dealers_attempted", "success_count", "error_count"],
                labels={
                    "observation_date": "Run date",
                    "duration_seconds": "Wall-clock seconds",
                },
            )
            st.plotly_chart(fig_runs, width="stretch")


# ---- Tab 3: Per-dealer comparison ------------------------------------------

with tab_dealer:
    if ts_window.is_empty():
        st.caption("Nothing to compare without successful observations.")
    else:
        by_dealer = (
            ts_window.group_by("dealer_code")
            .agg(
                pl.col("lead_time_hours").mean().alias("avg_lead_h"),
                pl.col("slot_count").mean().alias("avg_slots"),
                pl.col("scheduling_flow_seconds").mean().alias("avg_flow_s"),
                pl.len().alias("successes"),
            )
            .sort("avg_flow_s")
        )

        # Attempt counts from raw → success_rate
        if not raw_window.is_empty():
            attempts = (
                raw_window.group_by("dealer_code")
                .agg(pl.len().alias("attempts"))
            )
            by_dealer = by_dealer.join(attempts, on="dealer_code", how="left")
            by_dealer = by_dealer.with_columns(
                (100 * pl.col("successes") / pl.col("attempts")).alias("success_pct")
            )
        else:
            by_dealer = by_dealer.with_columns(
                pl.lit(100.0).alias("success_pct"),
                pl.col("successes").alias("attempts"),
            )

        if not registry.is_empty():
            by_dealer = by_dealer.join(
                registry, on="dealer_code", how="left"
            )

        st.subheader("Ranked dealer table")
        display_cols = [c for c in [
            "dealer_code", "dealer_name", "platform", "region",
            "avg_lead_h", "avg_slots", "avg_flow_s",
            "success_pct", "successes", "attempts",
        ] if c in by_dealer.columns]
        st.dataframe(
            by_dealer.select(display_cols).to_pandas(),
            hide_index=True,
            width="stretch",
            column_config={
                "avg_lead_h": st.column_config.NumberColumn("Avg lead (h)", format="%.1f"),
                "avg_slots": st.column_config.NumberColumn("Avg slots", format="%.0f"),
                "avg_flow_s": st.column_config.NumberColumn("Avg flow (s)", format="%.1f"),
                "success_pct": st.column_config.NumberColumn("Success %", format="%.0f"),
            },
        )

        st.subheader("Friction vs availability")
        st.caption(
            "Lower-left = fast page, few slots. Upper-right = slow page, many slots. "
            "Bubble size = success rate."
        )
        scatter_df = by_dealer.filter(
            pl.col("avg_flow_s").is_not_null()
        ).to_pandas()
        if scatter_df.empty:
            st.caption("Need flow-time data to plot the scatter.")
        else:
            fig_scatter = px.scatter(
                scatter_df,
                x="avg_flow_s", y="avg_slots",
                color="platform" if "platform" in scatter_df.columns else None,
                size="success_pct",
                hover_name="dealer_code",
                hover_data={
                    "avg_lead_h": ":.1f",
                    "successes": True,
                    "attempts": True,
                },
                labels={
                    "avg_flow_s": "Avg scheduling flow seconds (lower = faster)",
                    "avg_slots": "Avg slot count (higher = more availability)",
                },
            )
            st.plotly_chart(fig_scatter, width="stretch")
