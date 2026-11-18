"""
Creates realistic sample data so you can test the bot right away.

It writes two files into the data/ folder:
  - products.csv       -> one row per product (stock on hand, supplier, cost, lead time...)
  - sales_history.csv  -> how many units of each product sold per day for the last 90 days

Run it with:   python generate_sample_data.py
Running it again creates a fresh random set of data.
"""

import random
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).parent / "data"
DAYS_OF_HISTORY = 90

SUPPLIERS = {
    # supplier name: typical lead time in days (how long delivery takes)
    "Atlas Packaging Co.": 7,
    "Northwind Components": 14,
    "Summit Office Supply": 5,
    "Pacific Electronics": 21,
    "Garden State Distributors": 10,
}

# name, category, supplier, unit cost ($), typical units sold per day, case pack size
PRODUCTS = [
    ("Corrugated Box - Small", "Packaging", "Atlas Packaging Co.", 0.85, 120, 50),
    ("Corrugated Box - Large", "Packaging", "Atlas Packaging Co.", 1.60, 80, 25),
    ("Bubble Wrap Roll 12in", "Packaging", "Atlas Packaging Co.", 14.50, 9, 6),
    ("Packing Tape 6-Pack", "Packaging", "Atlas Packaging Co.", 11.00, 15, 12),
    ("Shipping Labels 500ct", "Packaging", "Atlas Packaging Co.", 18.75, 6, 10),
    ("Steel Bolt M8", "Hardware", "Northwind Components", 0.12, 400, 500),
    ("Hex Nut M8", "Hardware", "Northwind Components", 0.05, 420, 1000),
    ("Hinge Assembly", "Hardware", "Northwind Components", 3.40, 35, 20),
    ("Aluminum Bracket", "Hardware", "Northwind Components", 6.25, 22, 10),
    ("Rubber Gasket Kit", "Hardware", "Northwind Components", 9.90, 12, 12),
    ("Printer Paper Case", "Office", "Summit Office Supply", 42.00, 4, 1),
    ("Toner Cartridge", "Office", "Summit Office Supply", 89.00, 1.5, 1),
    ("Nitrile Gloves Box", "Office", "Summit Office Supply", 8.50, 18, 10),
    ("Safety Glasses", "Office", "Summit Office Supply", 4.25, 7, 12),
    ("Barcode Scanner", "Electronics", "Pacific Electronics", 145.00, 0.6, 1),
    ("Thermal Label Printer", "Electronics", "Pacific Electronics", 289.00, 0.3, 1),
    ("USB-C Cable 6ft", "Electronics", "Pacific Electronics", 5.75, 25, 20),
    ("Lithium Battery Pack", "Electronics", "Pacific Electronics", 32.00, 8, 4),
    ("Wireless Handheld Terminal", "Electronics", "Pacific Electronics", 610.00, 0.2, 1),
    ("Pallet Wrap Film", "Warehouse", "Garden State Distributors", 24.00, 10, 4),
    ("Wooden Pallet 48x40", "Warehouse", "Garden State Distributors", 12.50, 30, 10),
    ("Hand Truck", "Warehouse", "Garden State Distributors", 95.00, 0.4, 1),
    ("Forklift Propane Tank", "Warehouse", "Garden State Distributors", 38.00, 3, 2),
    ("Floor Marking Tape", "Warehouse", "Garden State Distributors", 16.00, 5, 6),
    ("Stretch Hood Bag", "Warehouse", "Garden State Distributors", 2.10, 60, 50),
]


def make_sales_history(today: date) -> pd.DataFrame:
    rows = []
    for name, _, _, _, avg_per_day, _ in PRODUCTS:
        trend = random.uniform(-0.25, 0.35)  # some products are growing, some shrinking
        for i in range(DAYS_OF_HISTORY):
            day = today - timedelta(days=DAYS_OF_HISTORY - i)
            progress = i / DAYS_OF_HISTORY
            expected = avg_per_day * (1 + trend * progress)
            if day.weekday() >= 5:  # weekends are slower
                expected *= 0.55
            # Poisson draws behave like real sales counts, even for slow sellers (0, 1, 2 per day)
            units = int(np.random.poisson(expected * random.uniform(0.75, 1.25)))
            rows.append({"date": day.isoformat(), "sku": sku_for(name), "units_sold": units})
    return pd.DataFrame(rows)


def make_products(sales: pd.DataFrame) -> pd.DataFrame:
    recent = sales.groupby("sku")["units_sold"].apply(lambda s: s.tail(30).mean())
    rows = []
    # Give every product a stock situation so the report always shows a realistic mix
    situations = ["out"] * 2 + ["critical"] * 3 + ["low"] * 5 + ["overstock"] * 3
    situations += ["healthy"] * (len(PRODUCTS) - len(situations))
    random.shuffle(situations)
    for (name, category, supplier, cost, _, case_pack), situation in zip(PRODUCTS, situations):
        sku = sku_for(name)
        lead_time = max(2, SUPPLIERS[supplier] + random.randint(-2, 3))
        daily = recent[sku]
        days_cover = {
            "out": 0,
            "critical": random.uniform(0.3, 0.6) * lead_time,
            "low": random.uniform(0.9, 1.2) * lead_time,
            "healthy": random.uniform(2.0, 4.0) * lead_time,
            "overstock": random.uniform(140, 220),  # days: above the bot's 120-day overstock line
        }[situation]
        on_hand = round(daily * days_cover)
        on_order = case_pack * random.randint(1, 4) if situation == "low" and random.random() < 0.3 else 0
        rows.append(
            {
                "sku": sku,
                "product_name": name,
                "category": category,
                "supplier": supplier,
                "unit_cost": cost,
                "on_hand": on_hand,
                "on_order": on_order,
                "lead_time_days": lead_time,
                "case_pack": case_pack,
            }
        )
    return pd.DataFrame(rows)


def sku_for(name: str) -> str:
    return "SKU-" + str(1000 + [p[0] for p in PRODUCTS].index(name))


def main():
    DATA_DIR.mkdir(exist_ok=True)
    today = date.today()
    sales = make_sales_history(today)
    products = make_products(sales)
    sales.to_csv(DATA_DIR / "sales_history.csv", index=False)
    products.to_csv(DATA_DIR / "products.csv", index=False)
    print(f"Created {len(products)} products and {len(sales)} days of sales rows in {DATA_DIR}/")


if __name__ == "__main__":
    main()
