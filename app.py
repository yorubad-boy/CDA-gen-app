import json
import re
from pathlib import Path

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="Community Finance Dashboard", page_icon="🏘️", layout="wide")

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]

CONFIG_PATH = Path(__file__).parent / "communities.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

# ---------------------------------------------------------------------------
# Self-onboarding registry — a Google Form response sheet where new
# communities submit their own sheet links. Set this to the registry
# spreadsheet's ID once you've created it (see setup instructions).
# Leave as None to disable self-onboarding and use communities.json only.
# ---------------------------------------------------------------------------
REGISTRY_SPREADSHEET_ID = None  # e.g. "1AbCдEfGhIjKlMnOpQrStUvWxYz..."
REGISTRY_GID = 0

# Maps the registry form's question labels -> (internal source name, sheet type)
REGISTRY_SOURCE_COLUMNS = {
    "development fee": ("Development Fee", "monthly"),
    "monthly minutes": ("Monthly Minutes", "monthly"),
    "electricity": ("Electricity Connection", "payment"),
    "projects": ("Projects", "payment"),
    "expenditure": ("Expenditure", "expenditure"),
    "bank withdrawal": ("Bank Withdrawal", "withdrawal"),
}


# ---------------------------------------------------------------------------
# Google auth (service account) — one client, reused for every sheet
# ---------------------------------------------------------------------------
@st.cache_resource
def get_gspread_client():
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


@st.cache_data(ttl=30)
def fetch_sheet_records(spreadsheet_id: str, gid: int | None) -> list[dict]:
    """Fetch a worksheet's rows as a list of dicts, authenticated as the service account."""
    client = get_gspread_client()
    sh = client.open_by_key(spreadsheet_id)
    ws = sh.get_worksheet_by_id(gid) if gid else sh.get_worksheet(0)
    return ws.get_all_records()


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "community"


def extract_spreadsheet_id(url_or_id: str) -> str:
    """Accepts a full Google Sheets share/edit URL or a bare spreadsheet ID."""
    if not url_or_id:
        return ""
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", str(url_or_id))
    return match.group(1) if match else str(url_or_id).strip()


def get_field(row: dict, keywords: list[str]):
    for key, value in row.items():
        low = str(key).lower()
        if any(kw in low for kw in keywords):
            return value
    return None


@st.cache_data(ttl=60)
def load_base_communities() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


@st.cache_data(ttl=60)
def load_registry_communities() -> dict:
    """Read self-submitted communities from the onboarding form's response sheet."""
    if not REGISTRY_SPREADSHEET_ID:
        return {}

    try:
        rows = fetch_sheet_records(REGISTRY_SPREADSHEET_ID, REGISTRY_GID)
    except Exception as e:
        st.warning(f"Couldn't load the community registry: {e}")
        return {}

    communities = {}
    for row in rows:
        name = get_field(row, ["community name", "community", "name"])
        if not name or not str(name).strip():
            continue
        name = str(name).strip()
        cid = slugify(name)

        icon = get_field(row, ["icon"]) or "🏘️"
        currency = get_field(row, ["currency"]) or "₦"

        sources = {}
        for keyword, (label, sheet_type) in REGISTRY_SOURCE_COLUMNS.items():
            link = get_field(row, [keyword])
            if link and str(link).strip():
                sources[label] = {
                    "spreadsheet_id": extract_spreadsheet_id(link),
                    "gid": 0,
                    "type": sheet_type,
                }

        if sources:
            communities[cid] = {
                "display_name": name,
                "icon": str(icon).strip(),
                "currency": str(currency).strip(),
                "sources": sources,
            }

    return communities


def load_communities() -> dict:
    """Base communities from communities.json, overlaid with self-onboarded ones."""
    combined = dict(load_base_communities())
    combined.update(load_registry_communities())
    return combined


def find_column(columns, keywords):
    for col in columns:
        low = str(col).lower()
        if any(kw in low for kw in keywords):
            return col
    return None


def load_monthly_sheet(spreadsheet_id: str, gid) -> pd.DataFrame:
    """Sheets shaped like: Timestamp | Year | Name | Jan..Dec"""
    records = fetch_sheet_records(spreadsheet_id, gid)
    df = pd.DataFrame(records)
    if df.empty:
        return pd.DataFrame(columns=["Timestamp", "Year", "Name"] + MONTHS + ["Total"])
    df.columns = [str(c).strip() for c in df.columns]

    keywords = {
        "Timestamp": ["timestamp"],
        "Year": ["year"],
        "Name": ["name"],
        **{m: [m.lower()] for m in MONTHS},
    }
    rename_map = {}
    for standard, kws in keywords.items():
        found = find_column(df.columns, kws)
        if found:
            rename_map[found] = standard
    df = df.rename(columns=rename_map)

    if "Name" not in df.columns:
        return pd.DataFrame(columns=["Timestamp", "Year", "Name"] + MONTHS + ["Total"])

    df = df[df["Name"].astype(str).str.strip() != ""].copy()
    df["Name"] = df["Name"].astype(str).str.strip().str.title()

    for m in MONTHS:
        if m not in df.columns:
            df[m] = 0
        df[m] = pd.to_numeric(df[m], errors="coerce").fillna(0)

    if "Year" in df.columns:
        df["Year"] = pd.to_numeric(df["Year"], errors="coerce").astype("Int64")
    if "Timestamp" in df.columns:
        df["Timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce")

    df["Total"] = df[MONTHS].sum(axis=1)
    keep = [c for c in ["Timestamp", "Year", "Name"] + MONTHS + ["Total"] if c in df.columns]
    return df[keep]


def load_payment_sheet(spreadsheet_id: str, gid) -> pd.DataFrame:
    """Sheets shaped like: Timestamp | Name | Payments Paid | Balance"""
    records = fetch_sheet_records(spreadsheet_id, gid)
    df = pd.DataFrame(records)
    if df.empty:
        return pd.DataFrame(columns=["Timestamp", "Name", "Paid", "Balance"])
    df.columns = [str(c).strip() for c in df.columns]

    keywords = {
        "Timestamp": ["timestamp"],
        "Name": ["name"],
        "Paid": ["payments paid", "paid"],
        "Balance": ["balance"],
    }
    rename_map = {}
    for standard, kws in keywords.items():
        found = find_column(df.columns, kws)
        if found:
            rename_map[found] = standard
    df = df.rename(columns=rename_map)

    if "Name" not in df.columns:
        return pd.DataFrame(columns=["Timestamp", "Name", "Paid", "Balance"])

    df = df[df["Name"].astype(str).str.strip() != ""].copy()
    df["Name"] = df["Name"].astype(str).str.strip().str.title()
    df["Paid"] = pd.to_numeric(df.get("Paid", 0), errors="coerce").fillna(0)
    df["Balance"] = pd.to_numeric(df.get("Balance", 0), errors="coerce").fillna(0)
    if "Timestamp" in df.columns:
        df["Timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce")

    keep = [c for c in ["Timestamp", "Name", "Paid", "Balance"] if c in df.columns]
    return df[keep]


def load_expenditure_sheet(spreadsheet_id: str, gid) -> pd.DataFrame:
    """Sheets shaped like: Timestamp | Items | Amount | Description"""
    records = fetch_sheet_records(spreadsheet_id, gid)
    df = pd.DataFrame(records)
    if df.empty:
        return pd.DataFrame(columns=["Timestamp", "Items", "Amount", "Description"])
    df.columns = [str(c).strip() for c in df.columns]

    keywords = {
        "Timestamp": ["timestamp"],
        "Items": ["item"],
        "Amount": ["amount"],
        "Description": ["description"],
    }
    rename_map = {}
    for standard, kws in keywords.items():
        found = find_column(df.columns, kws)
        if found:
            rename_map[found] = standard
    df = df.rename(columns=rename_map)

    if "Items" not in df.columns:
        return pd.DataFrame(columns=["Timestamp", "Items", "Amount", "Description"])

    df = df[df["Items"].astype(str).str.strip() != ""].copy()
    df["Items"] = df["Items"].astype(str).str.strip().str.title()
    df["Amount"] = pd.to_numeric(df.get("Amount", 0), errors="coerce").fillna(0)
    if "Description" in df.columns:
        df["Description"] = df["Description"].astype(str)
    if "Timestamp" in df.columns:
        df["Timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce")

    keep = [c for c in ["Timestamp", "Items", "Amount", "Description"] if c in df.columns]
    return df[keep]


def load_withdrawal_sheet(spreadsheet_id: str, gid) -> pd.DataFrame:
    """Sheets shaped like: Timestamp | Date | Cheque Number | Amount Withdrawn | Remark"""
    records = fetch_sheet_records(spreadsheet_id, gid)
    df = pd.DataFrame(records)
    if df.empty:
        return pd.DataFrame(columns=["Timestamp", "Date", "Cheque Number", "Amount", "Remark"])
    df.columns = [str(c).strip() for c in df.columns]

    keywords = {
        "Timestamp": ["timestamp"],
        "Date": ["date"],
        "Cheque Number": ["cheque", "check"],
        "Amount": ["amount"],
        "Remark": ["remark"],
    }
    rename_map = {}
    for standard, kws in keywords.items():
        found = find_column(df.columns, kws)
        if found:
            rename_map[found] = standard
    df = df.rename(columns=rename_map)

    if "Cheque Number" not in df.columns:
        return pd.DataFrame(columns=["Timestamp", "Date", "Cheque Number", "Amount", "Remark"])

    df = df[df["Cheque Number"].astype(str).str.strip() != ""].copy()
    df["Cheque Number"] = df["Cheque Number"].astype(str).str.strip()
    df["Amount"] = pd.to_numeric(df.get("Amount", 0), errors="coerce").fillna(0)
    if "Remark" in df.columns:
        df["Remark"] = df["Remark"].astype(str)
    if "Timestamp" in df.columns:
        df["Timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce")
    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")

    keep = [c for c in ["Timestamp", "Date", "Cheque Number", "Amount", "Remark"] if c in df.columns]
    return df[keep]


LOADERS = {
    "monthly": load_monthly_sheet,
    "payment": load_payment_sheet,
    "expenditure": load_expenditure_sheet,
    "withdrawal": load_withdrawal_sheet,
}

# Types included in the Overview summary. "expenditure" is intentionally left
# out — it's a standalone tab, not part of the combined totals.
SUMMARY_TYPES = {"monthly", "payment"}


def load_all(sources: dict):
    data = {}
    for label, cfg in sources.items():
        loader = LOADERS[cfg["type"]]
        try:
            data[label] = loader(cfg["spreadsheet_id"], cfg.get("gid"))
        except Exception as e:
            st.error(f"Couldn't load '{label}': {e}")
            data[label] = pd.DataFrame()
    return data


# ---------------------------------------------------------------------------
# Community selection
# ---------------------------------------------------------------------------
communities = load_communities()

if not communities:
    st.error("No communities configured yet. Add an entry to communities.json.")
    st.stop()

community_ids = list(communities.keys())
query_community = st.query_params.get("community")

default_index = community_ids.index(query_community) if query_community in community_ids else 0

with st.sidebar:
    st.header("Community")
    selected_id = st.selectbox(
        "Choose a community",
        community_ids,
        index=default_index,
        format_func=lambda cid: communities[cid]["display_name"],
    )

st.query_params["community"] = selected_id

community = communities[selected_id]
sources = community["sources"]
currency = community.get("currency", "$")
icon = community.get("icon", "🏘️")

st.title(f"{icon} {community['display_name']} — Finance Dashboard")
st.caption("To record a new entry, use the relevant Google Form. This page just shows the totals, pulled live and securely from each response sheet.")

data = load_all(sources)

if st.button("🔄 Refresh all data"):
    fetch_sheet_records.clear()
    load_base_communities.clear()
    load_registry_communities.clear()
    st.rerun()

# ---------------------------------------------------------------------------
# Overview tab — combined totals across all sheets for this community
# ---------------------------------------------------------------------------
tab_names = ["Overview"] + list(sources.keys())
tabs = st.tabs(tab_names)

with tabs[0]:
    # --- Filters ---
    all_names = set()
    all_years = set()
    for label, cfg in sources.items():
        df = data[label]
        if df.empty:
            continue
        if "Name" in df.columns:
            all_names.update(df["Name"].dropna().unique().tolist())
        if "Year" in df.columns:
            all_years.update(int(y) for y in df["Year"].dropna().unique().tolist())
        date_col = "Date" if "Date" in df.columns else ("Timestamp" if "Timestamp" in df.columns else None)
        if date_col:
            valid_dates = df[date_col].dropna()
            if not valid_dates.empty:
                all_years.update(valid_dates.dt.year.unique().tolist())

    fcol1, fcol2 = st.columns([2, 1])
    with fcol1:
        selected_years = st.multiselect(
            "Year",
            sorted(all_years, reverse=True),
            default=[],
            placeholder="All years",
            key=f"year_filter_{selected_id}",
        )
    with fcol2:
        selected_names = st.multiselect(
            "Person",
            sorted(all_names),
            default=[],
            placeholder="All members",
            key=f"person_filter_{selected_id}",
        )

    def apply_overview_filters(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        filtered = df
        if selected_years:
            if "Year" in filtered.columns:
                year_series = filtered["Year"]
            else:
                date_col = "Date" if "Date" in filtered.columns else ("Timestamp" if "Timestamp" in filtered.columns else None)
                year_series = filtered[date_col].dt.year if date_col else None
            if year_series is not None:
                # Rows with no year info at all are kept rather than silently dropped
                mask = year_series.isin(selected_years) | year_series.isna()
                filtered = filtered[mask]
        if selected_names and "Name" in filtered.columns:
            filtered = filtered[filtered["Name"].isin(selected_names)]
        return filtered

    overview_data = {label: apply_overview_filters(df) for label, df in data.items()}

    st.divider()

    monthly_income = sum(overview_data[label]["Total"].sum() for label in sources if sources[label]["type"] == "monthly" and not overview_data[label].empty)
    payments_paid = sum(overview_data[label]["Paid"].sum() for label in sources if sources[label]["type"] == "payment" and not overview_data[label].empty)
    outstanding_balance = sum(overview_data[label]["Balance"].sum() for label in sources if sources[label]["type"] == "payment" and not overview_data[label].empty)
    grand_total = monthly_income + payments_paid

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Recurring Income", f"{currency}{monthly_income:,.2f}")
    col2.metric("One-off Payments", f"{currency}{payments_paid:,.2f}")
    col3.metric("Outstanding Balance", f"{currency}{outstanding_balance:,.2f}")
    col4.metric("Grand Total Collected", f"{currency}{grand_total:,.2f}")

    st.divider()
    st.subheader("Collected by Source")
    summary_labels = [label for label in sources if sources[label]["type"] in ("monthly", "payment")]
    by_source = pd.DataFrame({
        "Source": summary_labels,
        "Amount": [
            overview_data[label]["Total"].sum() if sources[label]["type"] == "monthly" and not overview_data[label].empty
            else (overview_data[label]["Paid"].sum() if not overview_data[label].empty else 0)
            for label in summary_labels
        ],
    }).set_index("Source")
    st.bar_chart(by_source)

    st.subheader("Total Contribution by Member (all sources combined)")
    summary_labels = [label for label in sources if sources[label]["type"] in ("monthly", "payment")]
    per_source_columns = {}
    for label in summary_labels:
        cfg = sources[label]
        df = overview_data[label]
        if df.empty:
            per_source_columns[label] = pd.Series(dtype=float)
            continue
        if cfg["type"] == "monthly":
            per_source_columns[label] = df.groupby("Name")["Total"].sum()
        else:
            per_source_columns[label] = df.groupby("Name")["Paid"].sum()

    if any(not s.empty for s in per_source_columns.values()):
        member_table = pd.DataFrame(per_source_columns).fillna(0)
        member_table["Total"] = member_table.sum(axis=1)
        member_table = member_table.sort_values("Total", ascending=False)
        member_table = member_table.reset_index().rename(columns={"index": "Name"})

        # Format all amount columns with the currency symbol for display
        display_table = member_table.copy()
        for col in display_table.columns:
            if col != "Name":
                display_table[col] = display_table[col].apply(lambda v: f"{currency}{v:,.2f}")

        st.dataframe(display_table, use_container_width=True, hide_index=True)
    else:
        st.info("No entries recorded yet across any sheet.")

# ---------------------------------------------------------------------------
# Individual tabs — one per sheet
# ---------------------------------------------------------------------------
for label, tab in zip(sources.keys(), tabs[1:]):
    with tab:
        df = data[label]
        cfg = sources[label]

        if df.empty:
            st.info("No entries recorded yet.")
            continue

        if cfg["type"] == "monthly":
            years = sorted(df["Year"].dropna().unique().tolist()) if "Year" in df.columns else []
            if years:
                selected_years = st.multiselect("Year", years, default=years, key=f"years_{selected_id}_{label}")
                df = df[df["Year"].isin(selected_years)]

            summary = df.groupby("Name", as_index=False)["Total"].sum().rename(columns={"Total": "Total Income"})
            summary = summary.sort_values("Total Income", ascending=False)
            st.dataframe(summary, use_container_width=True, hide_index=True)

            c1, c2, c3 = st.columns(3)
            c1.metric("Total", f"{currency}{summary['Total Income'].sum():,.2f}")
            c2.metric("Entries", len(df))
            c3.metric("Members", summary["Name"].nunique())

            st.caption("By Member")
            st.bar_chart(summary.set_index("Name")["Total Income"])

            st.caption("By Month")
            monthly = df[MONTHS].sum()
            monthly.index = pd.CategoricalIndex(monthly.index, categories=MONTHS, ordered=True)
            st.bar_chart(monthly.sort_index())

        elif cfg["type"] == "payment":
            if "Timestamp" in df.columns:
                latest = df.sort_values("Timestamp").groupby("Name", as_index=False).last()
            else:
                latest = df.groupby("Name", as_index=False).last()

            total_paid = df.groupby("Name", as_index=False)["Paid"].sum().rename(columns={"Paid": "Total Paid"})
            summary = latest[["Name", "Balance"]].merge(total_paid, on="Name")
            summary = summary.sort_values("Balance", ascending=False)
            st.dataframe(summary, use_container_width=True, hide_index=True)

            c1, c2, c3 = st.columns(3)
            c1.metric("Total Paid", f"{currency}{summary['Total Paid'].sum():,.2f}")
            c2.metric("Outstanding Balance", f"{currency}{summary['Balance'].sum():,.2f}")
            c3.metric("Members Fully Paid", int((summary["Balance"] <= 0).sum()))

            st.caption("Outstanding Balance by Member")
            st.bar_chart(summary.set_index("Name")["Balance"])

        elif cfg["type"] == "expenditure":
            c1, c2 = st.columns(2)
            c1.metric("Total Spent", f"{currency}{df['Amount'].sum():,.2f}")
            c2.metric("Entries", len(df))

            by_item = df.groupby("Items", as_index=False)["Amount"].sum().sort_values("Amount", ascending=False)
            st.dataframe(by_item.rename(columns={"Amount": "Total Amount"}), use_container_width=True, hide_index=True)

            st.caption("Spend by Item")
            st.bar_chart(by_item.set_index("Items")["Amount"])

        elif cfg["type"] == "withdrawal":
            if "Date" in df.columns:
                df = df.sort_values("Date", ascending=False)

            c1, c2, c3 = st.columns(3)
            c1.metric("Total Withdrawn", f"{currency}{df['Amount'].sum():,.2f}")
            c2.metric("Number of Withdrawals", len(df))
            c3.metric("Average Withdrawal", f"{currency}{df['Amount'].mean():,.2f}")

            if "Date" in df.columns:
                st.caption("Withdrawals Over Time")
                by_date = df.groupby(df["Date"].dt.date)["Amount"].sum()
                st.bar_chart(by_date)

        with st.expander("Raw data"):
            st.dataframe(df, use_container_width=True, hide_index=True)
