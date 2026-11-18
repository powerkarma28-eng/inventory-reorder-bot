"""
Inventory Reorder Bot
=====================
Checks stock levels, forecasts demand, decides what needs to be reordered,
builds purchase order suggestions by supplier, measures how accurate its own
forecast has been, then:
  1. Sends an alert to Discord
  2. Saves a formatted Excel report
  3. Adds this run to a running history file (for trend analysis later)

Run it with:
    python bot.py               -> full run (Excel + Discord)
    python bot.py --no-discord  -> Excel only, prints the Discord message instead
"""

import argparse
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd
import requests
from openpyxl import load_workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import config

BASE_DIR = Path(__file__).parent

# Status labels, from most to least urgent
OUT = "OUT OF STOCK"
CRITICAL = "CRITICAL"
REORDER = "REORDER"
OK = "OK"
OVERSTOCK = "OVERSTOCK"
NO_DATA = "NO SALES DATA"  # new item with no sales history yet: a person should review it
STATUS_ORDER = [OUT, CRITICAL, REORDER, OK, OVERSTOCK, NO_DATA]

STATUS_COLORS = {  # Excel fill colors
    OUT: "F8CBAD",
    CRITICAL: "FCE4D6",
    REORDER: "FFF2CC",
    OK: "E2EFDA",
    OVERSTOCK: "DDEBF7",
    NO_DATA: "EDEDED",
}
STATUS_EMOJI = {OUT: "🔴", CRITICAL: "🟠", REORDER: "🟡", OK: "🟢", OVERSTOCK: "🔵", NO_DATA: "⚪"}

# The first version of this bot smoothed DAILY sales with alpha 0.2. The backtest keeps it
# as a comparison so the report shows whether the weekly method is actually better.
V1_DAILY_ALPHA = 0.2


# ---------------------------------------------------------------------------
# 1. Load data
# ---------------------------------------------------------------------------
def load_env_file():
    """Reads DISCORD_WEBHOOK_URL (and anything else) from a .env file if one exists."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_data():
    products_path = BASE_DIR / config.PRODUCTS_FILE
    sales_path = BASE_DIR / config.SALES_HISTORY_FILE
    if not products_path.exists() or not sales_path.exists():
        sys.exit(
            "Could not find the data files.\n"
            "Run this first to create sample data:  python generate_sample_data.py"
        )
    products = pd.read_csv(products_path)
    sales = pd.read_csv(sales_path, parse_dates=["date"])
    return products, sales


# ---------------------------------------------------------------------------
# 2. Forecast demand
# ---------------------------------------------------------------------------
def exponential_smoothing(values, alpha):
    """Simple exponential smoothing: recent periods count more than older ones."""
    forecast = values[0]
    for v in values[1:]:
        forecast = alpha * v + (1 - alpha) * forecast
    return forecast


def one_step_errors(values, alpha):
    """Forecast each period from the periods before it, and return the misses.
    Safety stock should cover how wrong the forecast tends to be, so these errors
    (not the raw ups and downs of sales) are what the safety stock formula uses."""
    errors = []
    forecast = values[0]
    for v in values[1:]:
        errors.append(v - forecast)
        forecast = alpha * v + (1 - alpha) * forecast
    return errors


def daily_matrix(sales: pd.DataFrame) -> pd.DataFrame:
    """One row per day, one column per SKU. Days with no sales row count as 0 sold."""
    s = sales.copy()
    s["date"] = pd.to_datetime(s["date"]).dt.normalize()
    table = s.pivot_table(index="date", columns="sku", values="units_sold", aggfunc="sum")
    all_days = pd.date_range(table.index.min(), table.index.max(), freq="D")
    return table.reindex(all_days).fillna(0)


def weekly_totals(daily: pd.Series) -> list:
    """Adds up complete 7-day blocks, counting back from the most recent day.
    Every block holds exactly one weekend, so the weekday the data ends on
    no longer pushes the forecast up or down."""
    weeks = len(daily) // 7
    if weeks == 0:
        return []
    recent = np.asarray(daily.iloc[-weeks * 7 :], dtype=float)
    return recent.reshape(weeks, 7).sum(axis=1).tolist()


def forecast_one(daily: pd.Series) -> dict:
    weeks = weekly_totals(daily)
    if len(weeks) >= config.MIN_WEEKS_FOR_FORECAST:
        alpha = config.SMOOTHING_ALPHA
        errors = one_step_errors(weeks, alpha)
        if len(errors) >= 3:
            weekly_error_std = math.sqrt(sum(e * e for e in errors) / len(errors))
        else:
            weekly_error_std = float(pd.Series(weeks).std(ddof=1))
        half = len(weeks) // 2
        first, second = np.mean(weeks[:half]), np.mean(weeks[half:])
        return {
            "avg_daily_sales": float(np.mean(weeks)) / 7,
            "forecast_daily_demand": exponential_smoothing(weeks, alpha) / 7,
            # A weekly spread converts to a daily one by dividing by the square root of 7
            "demand_std_dev": weekly_error_std / math.sqrt(7),
            "demand_trend_pct": (second - first) / first if first > 0 else 0.0,
            "forecast_method": "weekly smoothing",
        }
    # Less than the minimum number of full weeks: a plain average is the honest option.
    values = daily.tolist()
    return {
        "avg_daily_sales": float(np.mean(values)),
        "forecast_daily_demand": float(np.mean(values)),
        "demand_std_dev": float(pd.Series(values).std(ddof=1)) if len(values) > 1 else 0.0,
        "demand_trend_pct": 0.0,
        "forecast_method": "short history (plain average)",
    }


def forecast_demand(sales: pd.DataFrame) -> pd.DataFrame:
    if sales.empty:
        return pd.DataFrame(columns=["sku", "avg_daily_sales", "forecast_daily_demand",
                                     "demand_std_dev", "demand_trend_pct", "forecast_method"])
    daily = daily_matrix(sales)
    return pd.DataFrame([{"sku": sku, **forecast_one(daily[sku])} for sku in daily.columns])


# ---------------------------------------------------------------------------
# 3. Forecast accuracy (backtest)
# ---------------------------------------------------------------------------
def backtest(sales: pd.DataFrame, products: pd.DataFrame):
    """Hides the last few weeks of sales, forecasts them from the weeks before,
    and compares against what actually sold. Three methods are scored:
      - this bot's weekly smoothing
      - the bot's first version (daily smoothing)
      - a naive guess: "next week = last week"
    Error is measured as WAPE (weighted absolute percentage error):
    total units missed / total units actually sold. Accuracy = 1 - WAPE."""
    hold = config.BACKTEST_WEEKS
    if sales.empty:
        return pd.DataFrame(), None
    daily = daily_matrix(sales)
    names = products.set_index("sku")["product_name"].to_dict() if len(products) else {}
    rows = []
    for sku in daily.columns:
        series = daily[sku]
        weeks = weekly_totals(series)
        if len(weeks) < hold + config.MIN_WEEKS_FOR_FORECAST:
            continue
        train_weeks, test_weeks = weeks[:-hold], weeks[-hold:]
        train_days = series.iloc[: len(series) - hold * 7].tolist()
        f_new = exponential_smoothing(train_weeks, config.SMOOTHING_ALPHA)
        f_v1 = exponential_smoothing(train_days, V1_DAILY_ALPHA) * 7
        f_naive = train_weeks[-1]
        actual = sum(test_weeks)
        rows.append({
            "sku": sku,
            "product_name": names.get(sku, ""),
            "actual_weekly_avg": actual / hold,
            "forecast_weekly": f_new,
            "err_new": sum(abs(a - f_new) for a in test_weeks),
            "err_v1": sum(abs(a - f_v1) for a in test_weeks),
            "err_naive": sum(abs(a - f_naive) for a in test_weeks),
            "bias_new": sum(f_new - a for a in test_weeks),
            "actual_total": actual,
        })
    if not rows:
        return pd.DataFrame(), None
    bt = pd.DataFrame(rows)
    total = bt["actual_total"].sum()
    if total <= 0:
        return pd.DataFrame(), None
    summary = {
        "weeks_tested": hold,
        "items_tested": len(bt),
        "accuracy": 1 - bt["err_new"].sum() / total,
        "accuracy_v1": 1 - bt["err_v1"].sum() / total,
        "accuracy_naive": 1 - bt["err_naive"].sum() / total,
        "bias": bt["bias_new"].sum() / total,
    }
    safe_actual = bt["actual_total"].where(bt["actual_total"] > 0)
    bt["wape"] = (bt["err_new"] / safe_actual).round(3)
    bt["wape_v1"] = (bt["err_v1"] / safe_actual).round(3)
    bt["wape_naive"] = (bt["err_naive"] / safe_actual).round(3)
    bt = bt[["sku", "product_name", "actual_weekly_avg", "forecast_weekly", "wape", "wape_v1", "wape_naive"]]
    return bt.sort_values("wape", ascending=False).reset_index(drop=True), summary


# ---------------------------------------------------------------------------
# 4. Inventory analysis
# ---------------------------------------------------------------------------
def analyze(products: pd.DataFrame, demand: pd.DataFrame) -> pd.DataFrame:
    df = products.merge(demand, on="sku", how="left")
    df["has_sales_history"] = df["forecast_method"].notna()
    df["forecast_method"] = df["forecast_method"].fillna("no sales data")
    for col in ["avg_daily_sales", "forecast_daily_demand", "demand_std_dev", "demand_trend_pct"]:
        df[col] = df[col].fillna(0.0)
    df["case_pack"] = df["case_pack"].fillna(1).clip(lower=1)
    df["on_order"] = df["on_order"].fillna(0)

    z = NormalDist().inv_cdf(config.SERVICE_LEVEL)  # 95% -> about 1.65

    # Safety stock: extra buffer for forecast misses during the delivery wait
    df["safety_stock"] = np.ceil(z * df["demand_std_dev"] * np.sqrt(df["lead_time_days"])).astype(int)

    # Reorder point: expected sales while waiting for delivery + safety stock
    df["reorder_point"] = np.ceil(df["forecast_daily_demand"] * df["lead_time_days"] + df["safety_stock"]).astype(int)

    # Inventory position counts stock already ordered but not yet delivered
    df["inventory_position"] = df["on_hand"] + df["on_order"]

    daily = df["forecast_daily_demand"].clip(lower=0.01)
    df["days_of_supply"] = (df["on_hand"] / daily).round(1)
    df["cover_days_incl_on_order"] = (df["inventory_position"] / daily).round(1)
    df["est_stockout_date"] = [
        "unknown" if not hist
        else (datetime.now() + timedelta(days=d)).strftime("%Y-%m-%d") if d < 365 else "365+ days"
        for d, hist in zip(df["days_of_supply"], df["has_sales_history"])
    ]

    # EOQ (Economic Order Quantity): the order size that balances ordering vs. storage costs.
    # An item with no cost has no holding cost, so EOQ can't be calculated: it is left at 0.
    annual_demand = df["forecast_daily_demand"] * 365
    holding_cost = df["unit_cost"] * config.ANNUAL_HOLDING_COST_RATE
    safe_holding = holding_cost.where(holding_cost > 0, 1)
    df["eoq"] = np.where(
        holding_cost > 0, np.sqrt(2 * annual_demand * config.ORDERING_COST_PER_ORDER / safe_holding), 0
    ).round()

    # ABC analysis: rank items by yearly spend
    df["annual_spend"] = (annual_demand * df["unit_cost"]).round(2)
    df = df.sort_values("annual_spend", ascending=False)
    total_spend = df["annual_spend"].sum() or 1
    cumulative_share = df["annual_spend"].cumsum() / total_spend
    df["abc_class"] = [
        "A" if s <= config.ABC_A_CUTOFF or i == 0 else "B" if s <= config.ABC_B_CUTOFF else "C"
        for i, s in enumerate(cumulative_share)
    ]

    df["status"] = df.apply(get_status, axis=1)

    # How much to order ("order up to" rule): bring stock up to the reorder point plus one EOQ,
    # rounded up to full cases because suppliers sell in case packs.
    def order_qty(row):
        if row["status"] not in (OUT, CRITICAL, REORDER):
            return 0
        order_up_to = row["reorder_point"] + row["eoq"]
        needed = max(order_up_to - row["inventory_position"], 1)
        return int(math.ceil(needed / row["case_pack"]) * row["case_pack"])  # always at least 1 case

    df["suggested_order_qty"] = df.apply(order_qty, axis=1)
    df["order_cost"] = (df["suggested_order_qty"] * df["unit_cost"]).round(2)

    df["status_rank"] = df["status"].apply(STATUS_ORDER.index)
    df = df.sort_values(["status_rank", "abc_class", "days_of_supply"]).drop(columns="status_rank")
    return df.reset_index(drop=True)


def get_status(row):
    if row["on_hand"] <= 0:
        return OUT
    if not row["has_sales_history"]:
        return NO_DATA
    # Critical: it will run out before a new order could arrive, or it's already into safety stock
    if (
        row["inventory_position"] <= row["safety_stock"]
        or row["cover_days_incl_on_order"] < row["lead_time_days"]
    ):
        return CRITICAL
    if row["inventory_position"] <= row["reorder_point"]:
        return REORDER
    if row["days_of_supply"] > config.OVERSTOCK_DAYS:
        return OVERSTOCK
    return OK


def build_purchase_orders(df: pd.DataFrame) -> pd.DataFrame:
    to_order = df[df["suggested_order_qty"] > 0]
    po = (
        to_order.groupby("supplier")
        .agg(
            items=("sku", "count"),
            total_units=("suggested_order_qty", "sum"),
            total_cost=("order_cost", "sum"),
            most_urgent=("status", lambda s: min(s, key=STATUS_ORDER.index)),
            lead_time_days=("lead_time_days", "max"),
        )
        .reset_index()
        .sort_values("total_cost", ascending=False)
    )
    today = datetime.now()
    po.insert(0, "po_number", [f"PO-{today:%Y%m%d}-{i + 1:02d}" for i in range(len(po))])
    po["expected_delivery"] = [(today + timedelta(days=int(d))).strftime("%Y-%m-%d") for d in po["lead_time_days"]]
    return po


# ---------------------------------------------------------------------------
# 5. Excel report
# ---------------------------------------------------------------------------
NICE_HEADERS = {
    "sku": "SKU",
    "product_name": "Product",
    "category": "Category",
    "supplier": "Supplier",
    "unit_cost": "Unit Cost",
    "on_hand": "On Hand",
    "on_order": "On Order",
    "lead_time_days": "Lead Time (days)",
    "case_pack": "Case Pack",
    "avg_daily_sales": "Avg Daily Sales",
    "forecast_daily_demand": "Forecast Daily Demand",
    "demand_std_dev": "Forecast Error (daily std dev)",
    "demand_trend_pct": "Demand Trend",
    "forecast_method": "Forecast Method",
    "has_sales_history": "Has Sales History",
    "safety_stock": "Safety Stock",
    "reorder_point": "Reorder Point",
    "inventory_position": "Inventory Position",
    "days_of_supply": "Days of Supply",
    "cover_days_incl_on_order": "Days of Cover (incl. on order)",
    "est_stockout_date": "Est. Stockout Date",
    "eoq": "EOQ",
    "annual_spend": "Annual Spend",
    "abc_class": "ABC Class",
    "status": "Status",
    "suggested_order_qty": "Suggested Order Qty",
    "order_cost": "Order Cost",
    "po_number": "PO Number",
    "items": "Items",
    "total_units": "Total Units",
    "total_cost": "Total Cost",
    "most_urgent": "Most Urgent",
    "expected_delivery": "Expected Delivery",
    "actual_weekly_avg": "Actual Weekly Sales (avg)",
    "forecast_weekly": "Forecast Weekly Sales",
    "wape": "Forecast Error (WAPE)",
    "wape_v1": "Error: Old Daily Method",
    "wape_naive": "Error: Naive (last week)",
}
MONEY_COLS = {"Unit Cost", "Annual Spend", "Order Cost", "Total Cost", "PO Total ($)"}
DECIMAL_COLS = {"Avg Daily Sales", "Forecast Daily Demand", "Forecast Error (daily std dev)",
                "Actual Weekly Sales (avg)", "Forecast Weekly Sales"}
PERCENT_COLS = {"Demand Trend"}
PLAIN_PERCENT_COLS = {"Forecast Error (WAPE)", "Error: Old Daily Method", "Error: Naive (last week)"}

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(bold=True, color="FFFFFF")
THIN = Side(style="thin", color="D9D9D9")


def style_table(ws):
    """Bold headers, number formats, status colors, filters, frozen header, column widths."""
    headers = [c.value for c in ws[1]]
    for cell in ws[1]:
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 32

    for row in ws.iter_rows(min_row=2):
        status = None
        for cell, header in zip(row, headers):
            cell.border = Border(bottom=THIN)
            if header in MONEY_COLS:
                cell.number_format = '"$"#,##0.00'
            elif header in DECIMAL_COLS:
                cell.number_format = "0.00"
            elif header in PERCENT_COLS:
                cell.number_format = "+0%;-0%;0%"
            elif header in PLAIN_PERCENT_COLS:
                cell.number_format = "0.0%"
            if header in ("Status", "Most Urgent"):
                status = cell.value
        if status in STATUS_COLORS:
            for cell in row:
                cell.fill = PatternFill("solid", fgColor=STATUS_COLORS[status])

    for i, header in enumerate(headers, start=1):
        longest = max([len(str(header))] + [len(str(c.value or "")) for c in ws[get_column_letter(i)][1:]])
        ws.column_dimensions[get_column_letter(i)].width = min(max(10, longest + 2), 32)

    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions


def write_excel_report(df, po, run_time, accuracy=None, accuracy_summary=None) -> Path:
    reports = BASE_DIR / config.REPORTS_FOLDER
    reports.mkdir(exist_ok=True)
    path = reports / f"reorder_report_{run_time:%Y-%m-%d_%H%M}.xlsx"

    alert_cols = [
        "status", "sku", "product_name", "supplier", "abc_class", "on_hand", "on_order",
        "reorder_point", "safety_stock", "days_of_supply", "lead_time_days", "est_stockout_date",
        "suggested_order_qty", "unit_cost", "order_cost",
    ]
    alerts = df[df["status"].isin([OUT, CRITICAL, REORDER, NO_DATA])][alert_cols]
    po_lines = df[df["suggested_order_qty"] > 0][
        ["supplier", "sku", "product_name", "suggested_order_qty", "case_pack", "unit_cost", "order_cost"]
    ].sort_values(["supplier", "order_cost"], ascending=[True, False])

    sheets = ["Reorder Alerts", "Purchase Orders", "PO Line Items", "All Items", "Settings"]
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame().to_excel(writer, sheet_name="Summary")
        alerts.rename(columns=NICE_HEADERS).to_excel(writer, sheet_name="Reorder Alerts", index=False)
        po.rename(columns=NICE_HEADERS).to_excel(writer, sheet_name="Purchase Orders", index=False)
        po_lines.rename(columns=NICE_HEADERS).to_excel(writer, sheet_name="PO Line Items", index=False)
        df.rename(columns=NICE_HEADERS).to_excel(writer, sheet_name="All Items", index=False)
        if accuracy is not None and len(accuracy):
            accuracy.rename(columns=NICE_HEADERS).to_excel(writer, sheet_name="Forecast Accuracy", index=False)
            sheets.insert(4, "Forecast Accuracy")
        settings = pd.DataFrame(
            [
                ("Service level", config.SERVICE_LEVEL),
                ("Forecast smoothing alpha (weekly)", config.SMOOTHING_ALPHA),
                ("Weeks held back to test the forecast", config.BACKTEST_WEEKS),
                ("Ordering cost per PO ($)", config.ORDERING_COST_PER_ORDER),
                ("Annual holding cost rate", config.ANNUAL_HOLDING_COST_RATE),
                ("ABC 'A' cutoff (share of spend)", config.ABC_A_CUTOFF),
                ("ABC 'B' cutoff (share of spend)", config.ABC_B_CUTOFF),
                ("Overstock threshold (days)", config.OVERSTOCK_DAYS),
            ],
            columns=["Setting", "Value"],
        )
        settings.to_excel(writer, sheet_name="Settings", index=False)

    wb = load_workbook(path)
    for name in sheets:
        style_table(wb[name])
    build_summary_sheet(wb["Summary"], df, po, run_time, accuracy_summary)
    wb.save(path)
    return path


def build_summary_sheet(ws, df, po, run_time, accuracy_summary=None):
    ws["A1"] = "Inventory Reorder Report"
    ws["A1"].font = Font(bold=True, size=18, color="1F3864")
    ws["A2"] = f"Generated {run_time:%B %d, %Y at %I:%M %p}"
    ws["A2"].font = Font(italic=True, color="595959")

    counts = df["status"].value_counts()
    overstock = df[df["status"] == OVERSTOCK]
    kpis = [
        ("Items checked", len(df), None),
        ("Need reorder (all urgency levels)", int((df["suggested_order_qty"] > 0).sum()), None),
        ("Out of stock", int(counts.get(OUT, 0)), None),
        ("Critical (runs out before a new order arrives)", int(counts.get(CRITICAL, 0)), None),
        ("Overstocked items", int(counts.get(OVERSTOCK, 0)), None),
        ("New items with no sales data", int(counts.get(NO_DATA, 0)), None),
        ("Purchase orders to send", len(po), None),
        ("Total suggested spend", float(po["total_cost"].sum()), '"$"#,##0.00'),
        ("Inventory value on hand", float((df["on_hand"] * df["unit_cost"]).sum()), '"$"#,##0.00'),
        ("Value tied up in overstock", float((overstock["on_hand"] * overstock["unit_cost"]).sum()), '"$"#,##0.00'),
    ]
    if accuracy_summary:
        weeks = accuracy_summary["weeks_tested"]
        kpis += [
            (f"Forecast accuracy (last {weeks} weeks)", accuracy_summary["accuracy"], "0.0%"),
            ("  Old daily method, same weeks", accuracy_summary["accuracy_v1"], "0.0%"),
            ("  Naive guess (next week = last week)", accuracy_summary["accuracy_naive"], "0.0%"),
            ("Forecast bias (+ over, - under)", accuracy_summary["bias"], "+0.0%;-0.0%;0.0%"),
        ]
    ws["A4"], ws["B4"] = "Key Numbers", ""
    ws["A4"].font = Font(bold=True, size=13)
    for i, (label, value, fmt) in enumerate(kpis, start=5):
        ws.cell(row=i, column=1, value=label)
        cell = ws.cell(row=i, column=2, value=value)
        cell.font = Font(bold=True)
        if fmt:
            cell.number_format = fmt

    # Status breakdown table + chart
    start = 5 + len(kpis) + 2
    ws.cell(row=start - 1, column=1, value="Items by Status").font = Font(bold=True, size=13)
    ws.cell(row=start, column=1, value="Status").font = HEADER_FONT
    ws.cell(row=start, column=2, value="Items").font = HEADER_FONT
    ws.cell(row=start, column=1).fill = ws.cell(row=start, column=2).fill = HEADER_FILL
    for i, status in enumerate(STATUS_ORDER, start=start + 1):
        ws.cell(row=i, column=1, value=status).fill = PatternFill("solid", fgColor=STATUS_COLORS[status])
        ws.cell(row=i, column=2, value=int(counts.get(status, 0)))

    chart = BarChart()
    chart.title = "Items by Status"
    chart.y_axis.title = "Items"
    chart.legend = None
    chart.add_data(Reference(ws, min_col=2, min_row=start, max_row=start + len(STATUS_ORDER)), titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=1, min_row=start + 1, max_row=start + len(STATUS_ORDER)))
    chart.height, chart.width = 7.5, 13
    ws.add_chart(chart, "D4")

    # Spend by supplier chart
    if len(po):
        s_start = start + len(STATUS_ORDER) + 3
        ws.cell(row=s_start - 1, column=1, value="Suggested Spend by Supplier").font = Font(bold=True, size=13)
        ws.cell(row=s_start, column=1, value="Supplier").font = HEADER_FONT
        ws.cell(row=s_start, column=2, value="PO Total ($)").font = HEADER_FONT
        ws.cell(row=s_start, column=1).fill = ws.cell(row=s_start, column=2).fill = HEADER_FILL
        for i, (_, r) in enumerate(po.iterrows(), start=s_start + 1):
            ws.cell(row=i, column=1, value=r["supplier"])
            ws.cell(row=i, column=2, value=float(r["total_cost"])).number_format = '"$"#,##0.00'
        chart2 = BarChart()
        chart2.type = "bar"
        chart2.title = "Suggested Spend by Supplier"
        chart2.legend = None
        chart2.add_data(Reference(ws, min_col=2, min_row=s_start, max_row=s_start + len(po)), titles_from_data=True)
        chart2.set_categories(Reference(ws, min_col=1, min_row=s_start + 1, max_row=s_start + len(po)))
        chart2.height, chart2.width = 7.5, 13
        ws.add_chart(chart2, "D22")

    ws.column_dimensions["A"].width = 46
    ws.column_dimensions["B"].width = 16


def append_history(df, po, run_time, accuracy_summary=None) -> Path:
    """Keeps one running Excel file with every run, so you can analyze trends over time."""
    path = BASE_DIR / config.REPORTS_FOLDER / "alert_history.xlsx"
    stamp = run_time.strftime("%Y-%m-%d %H:%M")

    run_row = pd.DataFrame(
        [{
            "Run Time": stamp,
            "Items Checked": len(df),
            "Items To Reorder": int((df["suggested_order_qty"] > 0).sum()),
            "Out Of Stock": int((df["status"] == OUT).sum()),
            "Critical": int((df["status"] == CRITICAL).sum()),
            "Overstock": int((df["status"] == OVERSTOCK).sum()),
            "Total Suggested Spend": round(float(po["total_cost"].sum()), 2),
            "Inventory Value On Hand": round(float((df["on_hand"] * df["unit_cost"]).sum()), 2),
            "Forecast Accuracy": round(accuracy_summary["accuracy"], 4) if accuracy_summary else None,
        }]
    )
    alert_rows = df[df["suggested_order_qty"] > 0][
        ["status", "sku", "product_name", "supplier", "abc_class", "on_hand", "reorder_point",
         "days_of_supply", "suggested_order_qty", "order_cost"]
    ].rename(columns=NICE_HEADERS)
    alert_rows.insert(0, "Run Time", stamp)

    if path.exists():
        old = pd.read_excel(path, sheet_name=None)
        run_row = pd.concat([old.get("Runs", pd.DataFrame()), run_row], ignore_index=True)
        alert_rows = pd.concat([old.get("Alerts", pd.DataFrame()), alert_rows], ignore_index=True)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        run_row.to_excel(writer, sheet_name="Runs", index=False)
        alert_rows.to_excel(writer, sheet_name="Alerts", index=False)
    wb = load_workbook(path)
    MONEY_COLS.update({"Total Suggested Spend", "Inventory Value On Hand"})
    PLAIN_PERCENT_COLS.add("Forecast Accuracy")
    for ws in wb.worksheets:
        style_table(ws)
    wb.save(path)
    return path


# ---------------------------------------------------------------------------
# 6. Discord alert
# ---------------------------------------------------------------------------
def build_discord_payloads(df, po, run_time, report_name, accuracy_summary=None):
    """Discord limits: 10 embeds per message, 25 fields per embed. We split if needed."""
    to_order = df[df["suggested_order_qty"] > 0]
    counts = df["status"].value_counts()

    accuracy_line = ""
    if accuracy_summary:
        accuracy_line = (
            f"🎯 Forecast accuracy, last {accuracy_summary['weeks_tested']} weeks: "
            f"**{accuracy_summary['accuracy']:.0%}**\n"
        )
    summary = {
        "title": "📦 Inventory Reorder Report",
        "description": (
            f"**{len(to_order)}** of {len(df)} items need reordering "
            f"across **{len(po)}** suppliers.\n"
            f"{STATUS_EMOJI[OUT]} Out of stock: **{counts.get(OUT, 0)}**  "
            f"{STATUS_EMOJI[CRITICAL]} Critical: **{counts.get(CRITICAL, 0)}**  "
            f"{STATUS_EMOJI[REORDER]} Reorder: **{counts.get(REORDER, 0)}**\n"
            f"💰 Total suggested spend: **${po['total_cost'].sum():,.2f}**\n"
            f"{accuracy_line}"
            f"📊 Full details saved to `{report_name}`"
        ),
        "color": 0xC00000 if counts.get(OUT, 0) else 0xED7D31 if len(to_order) else 0x70AD47,
        "timestamp": run_time.astimezone().isoformat(),
    }
    if not len(to_order):
        summary["description"] = f"✅ All {len(df)} items are above their reorder points. Nothing to order today."
    if counts.get(NO_DATA, 0):
        summary["description"] += f"\n{STATUS_EMOJI[NO_DATA]} {counts[NO_DATA]} new item(s) have no sales data yet: review by hand."

    embeds = [summary]
    for _, p in po.iterrows():
        items = to_order[to_order["supplier"] == p["supplier"]]
        fields = []
        for _, r in items.head(25).iterrows():
            fields.append({
                "name": f"{STATUS_EMOJI[r['status']]} {r['product_name']} ({r['sku']})"[:256],
                "value": (
                    f"On hand: **{int(r['on_hand'])}** | Reorder pt: {int(r['reorder_point'])}\n"
                    f"Runs out: {r['est_stockout_date']} ({r['days_of_supply']} days, "
                    f"delivery takes {int(r['lead_time_days'])})\n"
                    f"Order **{int(r['suggested_order_qty'])}** units = ${r['order_cost']:,.2f}"
                ),
                "inline": False,
            })
        embeds.append({
            "title": f"🧾 {p['po_number']} — {p['supplier']}"[:256],
            "description": (
                f"{int(p['items'])} items · ${p['total_cost']:,.2f} · "
                f"lead time {int(p['lead_time_days'])} days · arrives ~{p['expected_delivery']}"
            ),
            "color": 0x2F5597,
            "fields": fields,
        })

    return [
        {"username": "Inventory Reorder Bot", "embeds": embeds[i : i + 10]}
        for i in range(0, len(embeds), 10)
    ]


def send_to_discord(payloads, webhook_url):
    for payload in payloads:
        response = requests.post(webhook_url, json=payload, timeout=15)
        if response.status_code == 429:  # rate limited: wait and try once more
            wait = response.json().get("retry_after", 2)
            import time
            time.sleep(float(wait))
            response = requests.post(webhook_url, json=payload, timeout=15)
        if response.status_code >= 300:
            print(f"⚠️  Discord returned an error ({response.status_code}): {response.text[:300]}")
            return False
    return True


def preview_discord(payloads):
    print("\n----- Discord message preview -----")
    for payload in payloads:
        for embed in payload["embeds"]:
            print(f"\n{embed['title']}\n{embed.get('description', '')}")
            for f in embed.get("fields", []):
                print(f"  • {f['name']}\n    " + f["value"].replace("\n", "\n    "))
    print("-----------------------------------\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Inventory Reorder Bot")
    parser.add_argument("--no-discord", action="store_true", help="Skip Discord and print the message instead")
    args = parser.parse_args()

    load_env_file()
    run_time = datetime.now()

    print("📥 Loading data...")
    products, sales = load_data()

    print("📈 Forecasting demand...")
    demand = forecast_demand(sales)

    print("🎯 Testing forecast accuracy on recent weeks...")
    accuracy, accuracy_summary = backtest(sales, products)
    if accuracy_summary:
        print(
            f"   Last {accuracy_summary['weeks_tested']} weeks: {accuracy_summary['accuracy']:.1%} accurate "
            f"(old daily method {accuracy_summary['accuracy_v1']:.1%}, "
            f"naive 'same as last week' {accuracy_summary['accuracy_naive']:.1%})"
        )
    else:
        print("   Not enough history to test yet (needs at least "
              f"{config.BACKTEST_WEEKS + config.MIN_WEEKS_FOR_FORECAST} full weeks).")

    print("🧮 Calculating safety stock, reorder points, and order quantities...")
    df = analyze(products, demand)
    po = build_purchase_orders(df)

    print("📊 Writing Excel report...")
    report_path = write_excel_report(df, po, run_time, accuracy, accuracy_summary)
    history_path = append_history(df, po, run_time, accuracy_summary)
    print(f"   Saved: {report_path.relative_to(BASE_DIR)}")
    print(f"   Updated history: {history_path.relative_to(BASE_DIR)}")

    payloads = build_discord_payloads(df, po, run_time, report_path.name, accuracy_summary)
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if args.no_discord or not webhook:
        if not args.no_discord:
            print("ℹ️  No DISCORD_WEBHOOK_URL found in .env, so here's what would have been sent:")
        preview_discord(payloads)
    else:
        print("💬 Sending Discord alert...")
        if send_to_discord(payloads, webhook):
            print("   Sent!")

    to_order = int((df["suggested_order_qty"] > 0).sum())
    print(f"✅ Done. {to_order} items need reordering, total suggested spend ${po['total_cost'].sum():,.2f}.")


if __name__ == "__main__":
    main()
