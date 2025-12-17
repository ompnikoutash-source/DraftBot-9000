import re
import time
from dataclasses import dataclass
from typing import Dict, List, Tuple

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

st.set_page_config(page_title="DraftBot 9000", layout="wide")

POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF"]
DEFAULT_DRAFT_ORDER = [
    "JAMES", "ERIC", "JACK", "TITO", "KYLE", "BEN",
    "DIEGO", "NIKO", "COLBY", "ALBERTO", "ROBERT", "NOAH"
]


@dataclass
class LeagueSettings:
    num_teams: int = 12
    starters: Dict[str, int] = None  # includes FLEX_RBWR
    run_window: int = 8
    history_year: int = 2025

    def __post_init__(self):
        if self.starters is None:
            # QB, WR, WR, RB, RB, W/R, TE, K, DEF
            self.starters = {
                "QB": 1,
                "RB": 2,
                "WR": 2,
                "TE": 1,
                "FLEX_RBWR": 1,
                "K": 1,
                "DEF": 1,
            }


def snake_team_index(pick_number: int, num_teams: int) -> int:
    round_number = (pick_number - 1) // num_teams + 1
    within = (pick_number - 1) % num_teams
    if round_number % 2 == 1:
        return within
    return num_teams - 1 - within


def next_pick_number(draft_log: pd.DataFrame) -> int:
    if draft_log.empty:
        return 1
    return int(draft_log["pick"].max()) + 1


def pick_to_round(pick: int, num_teams: int) -> int:
    return (pick - 1) // num_teams + 1


def _clean_raw_name(raw: str) -> str:
    return str(raw).replace("View News", "").strip()


def _first_existing_col(df: pd.DataFrame, candidates: List[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def parse_player_projections_from_workbook(xlsx_path: str) -> pd.DataFrame:
    """
    Parses the Player Projections sheet into:
    player, pos, team, proj_points, player_id

    Handles concatenated strings in the first column like:
    "Lamar JacksonQB - BAL"
    """
    df = pd.read_excel(xlsx_path, sheet_name="Player Projections")
    first_col = df.columns[0]

    # Locate section headers. Your updated sheet is organized by position sections.
    player_headers = df.index[df[first_col].astype(str).str.strip().eq("Player")].tolist()
    def_header_idx = df.index[df[first_col].astype(str).str.strip().eq("Team")].tolist()
    def_header_idx = def_header_idx[0] if def_header_idx else None

    if len(player_headers) < 4:
        raise ValueError("Could not find enough 'Player' section headers in Player Projections.")

    # Build ranges between section headers. Assumes order QB, RB, WR, TE, K in the sheet.
    ranges: List[Tuple[str, int, int]] = []
    if len(player_headers) >= 5:
        ranges.append(("QB", player_headers[0] + 1, player_headers[1] - 1))
        ranges.append(("RB", player_headers[1] + 1, player_headers[2] - 1))
        ranges.append(("WR", player_headers[2] + 1, player_headers[3] - 1))
        ranges.append(("TE", player_headers[3] + 1, player_headers[4] - 1))
        k_start = player_headers[4] + 1
        k_end = (def_header_idx - 1) if def_header_idx is not None else (len(df) - 1)
        ranges.append(("K", k_start, k_end))
    else:
        ranges.append(("QB", player_headers[0] + 1, len(df) - 1))

    if def_header_idx is not None:
        ranges.append(("DEF", def_header_idx + 1, len(df) - 1))

    out = []

    for pos, start, end in ranges:
        sub = df.iloc[start:end + 1].copy()
        sub = sub[sub[first_col].notna()]
        if sub.empty:
            continue

        # Points column differs by section.
        # QB/RB/WR/TE: usually "Fantasy"
        # K: often an unnamed points column
        # DEF: often "Ret" in your sheet
        if pos in ["QB", "RB", "WR", "TE"]:
            pts_col = _first_existing_col(sub, ["Fantasy", "Points", "Proj", "XFP", "FPPG"])
        elif pos == "K":
            pts_col = _first_existing_col(sub, ["Points", "Fantasy", "XFP", "FPPG", "Unnamed: 7", "Unnamed: 6", "Unnamed: 5"])
        else:  # DEF
            pts_col = _first_existing_col(sub, ["Ret", "Points", "Fantasy", "XFP", "FPPG"])

        # Fallback: pick the most numeric-looking column (besides first)
        if pts_col is None:
            numeric_cols = []
            for c in sub.columns[1:]:
                s = pd.to_numeric(sub[c], errors="coerce")
                numeric_cols.append((c, s.notna().mean()))
            numeric_cols = sorted(numeric_cols, key=lambda x: x[1], reverse=True)
            pts_col = numeric_cols[0][0] if numeric_cols else None

        if pts_col is None:
            continue

        for _, r in sub.iterrows():
            raw = _clean_raw_name(r[first_col])

            team = None
            left = raw
            if " - " in raw:
                left, team = raw.split(" - ", 1)
                team = team.strip()

            m = re.search(r"(QB|RB|WR|TE|K|DEF)$", left.strip())
            if m:
                detected_pos = m.group(1)
                name = left[:m.start()].strip()
            else:
                detected_pos = pos
                name = left.strip()

            pts = r.get(pts_col, np.nan)
            try:
                pts = float(pts)
            except Exception:
                pts = np.nan

            if pd.isna(pts):
                continue

            final_pos = detected_pos if detected_pos in POSITIONS else pos
            if final_pos not in POSITIONS:
                continue

            out.append(
                {
                    "player": name,
                    "pos": final_pos,
                    "team": team,
                    "proj_points": pts,
                }
            )

    pool = pd.DataFrame(out)
    pool["team"] = pool["team"].fillna("").astype(str).str.strip()
    pool["player"] = pool["player"].fillna("").astype(str).str.strip()

    pool["player_id"] = (
        pool["player"].str.lower().str.replace(r"[^a-z0-9]+", "_", regex=True).str.strip("_")
        + "_"
        + pool["pos"]
    )

    pool = pool[pool["pos"].isin(POSITIONS)].drop_duplicates(subset=["player_id"]).reset_index(drop=True)
    return pool


def parse_previous_draft_results(xlsx_path: str) -> Dict[int, pd.DataFrame]:
    """
    Returns blocks: year -> DataFrame with Round + owner columns (positions per round).
    """
    df = pd.read_excel(xlsx_path, sheet_name="Previous Draft Results", header=None)

    years = []
    for idx, val in df[0].items():
        if pd.notna(val) and isinstance(val, (int, float)):
            iv = int(val)
            if 1900 <= iv <= 2100 and float(val).is_integer():
                years.append((iv, idx))

    blocks: Dict[int, pd.DataFrame] = {}
    for i, (yr, start_idx) in enumerate(years):
        end_idx = years[i + 1][1] - 1 if i + 1 < len(years) else len(df) - 1
        block = df.iloc[start_idx:end_idx + 1].copy()

        owners = block.iloc[0, 1:13].tolist()
        rounds = block.iloc[1:, 0]
        mat = block.iloc[1:, 1:13]

        valid = rounds.notna()
        rounds = rounds[valid].astype(int)
        mat = mat[valid]

        mat.columns = owners
        mat.insert(0, "Round", rounds.values)
        blocks[yr] = mat.reset_index(drop=True)

    return blocks


def compute_vorp_with_flex(pool: pd.DataFrame, settings: LeagueSettings) -> Tuple[pd.DataFrame, Dict[str, float]]:
    """
    VORP with RB/WR FLEX handled correctly:
    - QB/TE/K/DEF replacement: starter cutoff
    - RB replacement: RB24 (12 teams * 2 RB)
    - WR replacement: WR24 (12 teams * 2 WR)
    - FLEX replacement: best remaining RB/WR at FLEX cutoff (12 teams * 1 flex)
    - RB/WR baseline = max(pos replacement, flex replacement)
    """
    df = pool.copy()

    def nth_points(pos: str, n: int) -> float:
        d = df[df["pos"] == pos].sort_values("proj_points", ascending=False).reset_index(drop=True)
        if len(d) == 0:
            return 0.0
        idx = min(n - 1, len(d) - 1)
        return float(d.loc[idx, "proj_points"])

    num_teams = settings.num_teams
    s = settings.starters

    repl: Dict[str, float] = {}
    repl["QB"] = nth_points("QB", num_teams * s["QB"])
    repl["TE"] = nth_points("TE", num_teams * s["TE"])
    repl["K"] = nth_points("K", num_teams * s["K"])
    repl["DEF"] = nth_points("DEF", num_teams * s["DEF"])

    rb_n = num_teams * s["RB"]
    wr_n = num_teams * s["WR"]
    repl["RB"] = nth_points("RB", rb_n)
    repl["WR"] = nth_points("WR", wr_n)

    rb_sorted = df[df["pos"] == "RB"].sort_values("proj_points", ascending=False).reset_index(drop=True)
    wr_sorted = df[df["pos"] == "WR"].sort_values("proj_points", ascending=False).reset_index(drop=True)

    rb_rem = rb_sorted.iloc[rb_n:] if len(rb_sorted) > rb_n else rb_sorted.iloc[0:0]
    wr_rem = wr_sorted.iloc[wr_n:] if len(wr_sorted) > wr_n else wr_sorted.iloc[0:0]

    flex_pool = pd.concat([rb_rem, wr_rem], ignore_index=True)
    flex_n = num_teams * s["FLEX_RBWR"]
    if len(flex_pool) == 0:
        repl["FLEX_RBWR"] = 0.0
    else:
        flex_pool = flex_pool.sort_values("proj_points", ascending=False).reset_index(drop=True)
        idx = min(flex_n - 1, len(flex_pool) - 1)
        repl["FLEX_RBWR"] = float(flex_pool.loc[idx, "proj_points"])

    def baseline(row) -> float:
        if row["pos"] in ["RB", "WR"]:
            return max(repl[row["pos"]], repl["FLEX_RBWR"])
        return repl.get(row["pos"], 0.0)

    df["replacement_points"] = df.apply(baseline, axis=1)
    df["vorp"] = df["proj_points"] - df["replacement_points"]
    return df, repl


def init_state():
    if "xlsx_path" not in st.session_state:
        st.session_state.xlsx_path = "DraftBot 9000.xlsx"

    if "settings" not in st.session_state:
        st.session_state.settings = LeagueSettings()

    if "draft_order" not in st.session_state:
        st.session_state.draft_order = DEFAULT_DRAFT_ORDER.copy()

    if "my_owner" not in st.session_state:
        st.session_state.my_owner = "NIKO"

    if "auto_advance" not in st.session_state:
        st.session_state.auto_advance = True

    if "draft_log" not in st.session_state:
        st.session_state.draft_log = pd.DataFrame(
            columns=["pick", "round", "owner", "player_id", "player", "pos", "team", "proj_points", "vorp", "ts"]
        )

    if "availability" not in st.session_state:
        st.session_state.availability = {}  # player_id -> bool

    if "current_owner" not in st.session_state:
        st.session_state.current_owner = st.session_state.draft_order[0]


@st.cache_data(show_spinner=False)
def load_workbook_data(xlsx_path: str):
    pool = parse_player_projections_from_workbook(xlsx_path)
    history_blocks = parse_previous_draft_results(xlsx_path)
    return pool, history_blocks


def ensure_availability(pool: pd.DataFrame):
    for pid in pool["player_id"].tolist():
        if pid not in st.session_state.availability:
            st.session_state.availability[pid] = True


def undo_last_pick():
    if st.session_state.draft_log.empty:
        return
    last = st.session_state.draft_log.iloc[-1]
    st.session_state.availability[last["player_id"]] = True
    st.session_state.draft_log = st.session_state.draft_log.iloc[:-1].reset_index(drop=True)

    # After undo, set current owner back to whoever made that pick
    st.session_state.current_owner = str(last["owner"]).strip()


def reset_draft(pool: pd.DataFrame, new_order: List[str]):
    st.session_state.draft_order = new_order
    st.session_state.current_owner = new_order[0]
    st.session_state.draft_log = st.session_state.draft_log.iloc[0:0].copy()
    st.session_state.availability = {pid: True for pid in pool["player_id"].tolist()}


def draft_player(row: pd.Series, owner: str):
    if not st.session_state.availability.get(row["player_id"], True):
        return

    pick = next_pick_number(st.session_state.draft_log)
    rnd = pick_to_round(pick, st.session_state.settings.num_teams)

    st.session_state.draft_log = pd.concat(
        [
            st.session_state.draft_log,
            pd.DataFrame(
                [
                    {
                        "pick": pick,
                        "round": rnd,
                        "owner": owner,
                        "player_id": row["player_id"],
                        "player": row["player"],
                        "pos": row["pos"],
                        "team": row["team"],
                        "proj_points": float(row["proj_points"]),
                        "vorp": float(row["vorp"]),
                        "ts": time.time(),
                    }
                ]
            ),
        ],
        ignore_index=True,
    )

    st.session_state.availability[row["player_id"]] = False

    if st.session_state.auto_advance:
        next_pick = pick + 1
        idx = snake_team_index(next_pick, st.session_state.settings.num_teams)
        st.session_state.current_owner = st.session_state.draft_order[idx]


def roster_counts(owner: str) -> Dict[str, int]:
    dl = st.session_state.draft_log
    mine = dl[dl["owner"] == owner]
    return mine["pos"].value_counts().to_dict()


def run_risk(settings: LeagueSettings, history: pd.DataFrame | None, draft_log: pd.DataFrame, available_df: pd.DataFrame) -> Dict[str, float]:
    """
    Severity 0..1 based on:
    - recent run behavior (last N picks)
    - historical tendency for current round (from Previous Draft Results)
    - scarcity among remaining players (top VORP thinning)
    """
    window = settings.run_window
    recent = draft_log.tail(window)
    recent_counts = recent["pos"].value_counts().to_dict()
    denom = max(1, min(window, len(recent)))
    recent_score = {p: recent_counts.get(p, 0) / denom for p in POSITIONS}

    if history is None or history.empty:
        hist_score = {p: 0.0 for p in POSITIONS}
    else:
        current_round = pick_to_round(next_pick_number(draft_log), settings.num_teams)
        row = history[history["Round"] == current_round]
        if len(row) == 1:
            vals = row.iloc[0].drop(labels=["Round"]).astype(str)
            counts = vals.value_counts().to_dict()
            hist_score = {p: counts.get(p, 0) / settings.num_teams for p in POSITIONS}
        else:
            hist_score = {p: 0.0 for p in POSITIONS}

    scarcity = {}
    for p in POSITIONS:
        top = available_df[(available_df["pos"] == p) & (available_df["available"])].nlargest(10, "vorp")
        scarcity[p] = 1.0 - min(len(top) / 10.0, 1.0)

    severity = {}
    for p in POSITIONS:
        severity[p] = float(np.clip(0.45 * recent_score[p] + 0.35 * hist_score[p] + 0.20 * scarcity[p], 0.0, 1.0))
    return severity


def best_available_by_pos(df: pd.DataFrame) -> pd.DataFrame:
    av = df[df["available"]].copy()
    rows = []
    for pos in POSITIONS:
        best = av[av["pos"] == pos].nlargest(1, "vorp")
        if len(best) == 1:
            r = best.iloc[0]
            rows.append({"pos": pos, "player": r["player"], "team": r["team"], "proj_points": r["proj_points"], "vorp": r["vorp"]})
        else:
            rows.append({"pos": pos, "player": None, "team": None, "proj_points": None, "vorp": None})
    return pd.DataFrame(rows)


def need_bonus(my_owner: str, settings: LeagueSettings) -> Dict[str, float]:
    counts = roster_counts(my_owner)
    targets = {
        "QB": settings.starters["QB"],
        "RB": settings.starters["RB"],
        "WR": settings.starters["WR"],
        "TE": settings.starters["TE"],
        "K": settings.starters["K"],
        "DEF": settings.starters["DEF"],
    }

    bonus = {p: 0.0 for p in POSITIONS}
    for p in POSITIONS:
        deficit = max(0, targets.get(p, 0) - counts.get(p, 0))
        bonus[p] = 1.0 * deficit

    # Small baseline bias for RB/WR due to FLEX and general bench utility
    bonus["RB"] += 0.25
    bonus["WR"] += 0.20
    return bonus


init_state()
settings: LeagueSettings = st.session_state.settings

# Global layout tighten + bubble styling
st.markdown(
    """
    <style>
      div[data-testid="stAppViewContainer"] > .main .block-container {
        padding-top: 0.75rem;
        padding-bottom: 1.0rem;
      }
      h1 { margin-top: 0.2rem; padding-top: 0rem; }
      div.stButton > button {
        border-radius: 999px;
        padding: 0.35rem 0.60rem;
        text-align: left;
        width: 100%;
        font-size: 12px;
        line-height: 1.15;
        white-space: normal;
        overflow-wrap: anywhere;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

# Sidebar
st.sidebar.header("Inputs")

st.session_state.xlsx_path = st.sidebar.text_input("Workbook filename", st.session_state.xlsx_path)

settings.run_window = st.sidebar.slider("Run detection window (picks)", 4, 16, settings.run_window)

st.session_state.my_owner = st.sidebar.selectbox("My owner", DEFAULT_DRAFT_ORDER, index=DEFAULT_DRAFT_ORDER.index(st.session_state.my_owner) if st.session_state.my_owner in DEFAULT_DRAFT_ORDER else 0)
st.session_state.auto_advance = st.sidebar.checkbox("Auto advance owner (snake)", value=st.session_state.auto_advance)

pool, history_blocks = load_workbook_data(st.session_state.xlsx_path)
ensure_availability(pool)

# Manual draft order input + Submit reset
st.sidebar.subheader("Draft order")
order_text = st.sidebar.text_area(
    "Enter owner order (comma or newline separated)",
    value="\n".join(st.session_state.draft_order),
    height=160,
)

if st.sidebar.button("Submit (reset draft)"):
    raw = [x.strip() for x in re.split(r"[,\n]+", order_text) if x.strip()]
    raw = [x.upper() for x in raw]
    if len(raw) != settings.num_teams:
        st.sidebar.error(f"Need exactly {settings.num_teams} owners. You entered {len(raw)}.")
    else:
        reset_draft(pool, raw)
        st.rerun()

# Optional history year selector for run tendencies only
year_options = sorted(list(history_blocks.keys()))
settings.history_year = st.sidebar.selectbox(
    "History year (run tendencies)",
    year_options,
    index=year_options.index(settings.history_year) if settings.history_year in year_options else 0,
)

# Title near top
st.title("DraftBot 9000")

# Compute VORP and availability
pool_vorp, repl = compute_vorp_with_flex(pool, settings)
pool_vorp["available"] = pool_vorp["player_id"].map(st.session_state.availability).fillna(True)

# Header row controls (all on one line)
pick = next_pick_number(st.session_state.draft_log)
rnd = pick_to_round(pick, settings.num_teams)

h1, h2, h3, h4, h5 = st.columns([1.1, 1.1, 3.0, 1.2, 1.4], vertical_alignment="center")

with h1:
    st.markdown(f"**Round:** {rnd}")
with h2:
    st.markdown(f"**Next Pick:** {pick}")
with h3:
    if st.session_state.current_owner not in st.session_state.draft_order:
        st.session_state.current_owner = st.session_state.draft_order[0]

    st.session_state.current_owner = st.selectbox(
        "Currently picking owner",
        st.session_state.draft_order,
        index=st.session_state.draft_order.index(st.session_state.current_owner),
        label_visibility="collapsed",
    )
with h4:
    st.button("Undo last pick", on_click=undo_last_pick)
with h5:
    if st.button("Advance to next owner"):
        next_p = next_pick_number(st.session_state.draft_log)
        idx = snake_team_index(next_p, settings.num_teams)
        st.session_state.current_owner = st.session_state.draft_order[idx]

# Main layout
left, right = st.columns([1.25, 1.0], gap="large")

with left:
    st.subheader("Player Bubbles")

    # Autocomplete quick pick
    st.markdown("#### Quick pick (type to search)")
    available_players = pool_vorp[pool_vorp["available"]].copy()
    available_players["label"] = (
        available_players["player"].fillna("")
        + " ("
        + available_players["team"].fillna("").astype(str)
        + ") | "
        + available_players["pos"].fillna("")
        + " | VORP "
        + available_players["vorp"].round(1).astype(str)
    )

    if not available_players.empty:
        choice = st.selectbox(
            "Type to search and select a player",
            options=available_players["player_id"].tolist(),
            format_func=lambda pid: available_players.loc[available_players["player_id"] == pid, "label"].iloc[0],
            label_visibility="collapsed",
        )

        qp1, qp2 = st.columns([1.0, 3.0], vertical_alignment="center")
        with qp1:
            if st.button("Draft selected"):
                rrow = available_players.loc[available_players["player_id"] == choice].iloc[0]
                draft_player(rrow, st.session_state.current_owner)
                st.rerun()
        with qp2:
            st.caption("This dropdown is autocomplete. Start typing a name and press Enter.")

    st.divider()

    # Search + tab view
    search = st.text_input("Search player (filters the lists below)", value="")
    sort_by = st.selectbox("Sort by", ["VORP", "Projected Points"], index=0)
    show_n = st.slider("Show top N per position", 10, 90, 35)

    tabs = st.tabs(POSITIONS)
    for i, pos in enumerate(POSITIONS):
        with tabs[i]:
            sub = pool_vorp[(pool_vorp["pos"] == pos) & (pool_vorp["available"])].copy()
            if search.strip():
                sub = sub[sub["player"].str.contains(search.strip(), case=False, na=False)]
            if sort_by == "VORP":
                sub = sub.sort_values("vorp", ascending=False)
            else:
                sub = sub.sort_values("proj_points", ascending=False)

            sub = sub.head(show_n)

            cols = st.columns(3)
            for j, (_, rrow) in enumerate(sub.iterrows()):
                label = f"{rrow['player']} ({rrow['team']}) | {pos} | VORP {rrow['vorp']:.1f}"
                with cols[j % 3]:
                    if st.button(label, key=f"{pos}_{rrow['player_id']}"):
                        draft_player(rrow, st.session_state.current_owner)
                        st.rerun()

with right:
    st.subheader("Draft Signals")

    dl = st.session_state.draft_log.copy()

    # Position dot chart (other owners)
    other = dl[dl["owner"] != st.session_state.my_owner].copy()
    if not other.empty:
        pos_map = {p: k for k, p in enumerate(POSITIONS)}
        other["pos_y"] = other["pos"].map(pos_map).astype(float)

        chart = (
            alt.Chart(other)
            .mark_circle(size=80)
            .encode(
                x=alt.X("pick:Q", title="Pick"),
                y=alt.Y(
                    "pos_y:Q",
                    title="",
                    axis=alt.Axis(
                        values=list(pos_map.values()),
                        labelExpr="['QB','RB','WR','TE','K','DEF'][datum.value]",
                    ),
                ),
                color=alt.Color("pos:N", title="Position"),
                tooltip=["pick", "round", "owner", "player", "pos", "vorp"],
            )
            .properties(height=190)
        )
        st.altair_chart(chart, use_container_width=True)
    else:
        st.info("No picks logged yet.")

    # Run risk bars
    st.markdown("### Run risk")
    hist = history_blocks.get(settings.history_year, None)
    sev = run_risk(settings, hist, dl, pool_vorp)

    for pos in POSITIONS:
        st.write(pos)
        st.progress(int(sev[pos] * 100))

    st.markdown("### Best available by position")
    panel = best_available_by_pos(pool_vorp)
    st.dataframe(panel, use_container_width=True, hide_index=True)

    st.markdown("### Best pick now")
    av = pool_vorp[pool_vorp["available"]].copy()
    if not av.empty:
        bonus = need_bonus(st.session_state.my_owner, settings)
        av["adjusted"] = av["vorp"] + av["pos"].map(bonus) + av["pos"].map(lambda p: 2.0 * sev[p])
        best = av.sort_values("adjusted", ascending=False).iloc[0]
        st.success(
            f"{best['player']} ({best['pos']}) | Adj {best['adjusted']:.1f} | VORP {best['vorp']:.1f} | Proj {best['proj_points']:.1f}"
        )
    else:
        st.warning("No available players remaining.")

    st.markdown("### My roster")
    my_dl = dl[dl["owner"] == st.session_state.my_owner].sort_values("pick")
    if my_dl.empty:
        st.caption("No picks yet.")
    else:
        st.dataframe(my_dl[["pick", "round", "player", "pos", "team", "proj_points", "vorp"]], use_container_width=True, hide_index=True)

st.divider()
st.subheader("Draft Log")
st.dataframe(st.session_state.draft_log.sort_values("pick"), use_container_width=True, hide_index=True)

st.caption(
    f"Replacement points: QB {repl['QB']:.1f} | RB {repl['RB']:.1f} | WR {repl['WR']:.1f} | "
    f"TE {repl['TE']:.1f} | FLEX {repl['FLEX_RBWR']:.1f} | K {repl['K']:.1f} | DEF {repl['DEF']:.1f}"
)
