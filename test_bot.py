"""
Automated tests for the Inventory Reorder Bot.

Run them with:   python -m unittest -v test_bot

Each test builds a small, hand-made situation where the right answer is known,
then checks that the bot gets it right.
"""

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

import bot


def sales_for(sku, days, end=date(2026, 11, 15), weekday_units=100, weekend_units=55):
    """A steady seller: the same sales every weekday, fewer on weekends."""
    rows = []
    for i in range(days):
        d = end - timedelta(days=days - 1 - i)
        units = weekend_units if d.weekday() >= 5 else weekday_units
        rows.append({"date": pd.Timestamp(d), "sku": sku, "units_sold": units})
    return pd.DataFrame(rows)


def product(sku="SKU-1", on_hand=500, on_order=0, lead=7, cost=2.0, case_pack=10, supplier="Acme"):
    return {"sku": sku, "product_name": f"Item {sku}", "category": "Test", "supplier": supplier,
            "unit_cost": cost, "on_hand": on_hand, "on_order": on_order,
            "lead_time_days": lead, "case_pack": case_pack}


def run(products, sales):
    return bot.analyze(pd.DataFrame(products), bot.forecast_demand(sales)).set_index("sku")


class ForecastTests(unittest.TestCase):
    def test_weekday_does_not_change_forecast(self):
        """Ending the data on a Sunday or a Wednesday must give the same forecast."""
        forecasts = []
        for end in [date(2026, 11, 15), date(2026, 11, 11)]:  # Sunday, Wednesday
            f = bot.forecast_demand(sales_for("SKU-1", 84, end=end)).iloc[0]
            forecasts.append(f["forecast_daily_demand"])
        expected = (5 * 100 + 2 * 55) / 7  # true average day, weekends included
        for f in forecasts:
            self.assertAlmostEqual(f, expected, places=6)

    def test_steady_seller_has_no_safety_stock_for_forecast_error(self):
        """If sales never change week to week, the forecast never misses, so no safety stock is needed."""
        df = run([product()], sales_for("SKU-1", 84))
        self.assertEqual(df.loc["SKU-1", "safety_stock"], 0)

    def test_short_history_uses_plain_average(self):
        f = bot.forecast_demand(sales_for("SKU-1", 5, weekday_units=10, weekend_units=10)).iloc[0]
        self.assertEqual(f["forecast_method"], "short history (plain average)")
        self.assertAlmostEqual(f["forecast_daily_demand"], 10)


class StatusTests(unittest.TestCase):
    """Daily demand in these tests is about 87 units (weekday 100, weekend 55)."""

    def test_out_of_stock(self):
        self.assertEqual(run([product(on_hand=0)], sales_for("SKU-1", 84)).loc["SKU-1", "status"], bot.OUT)

    def test_runs_out_before_delivery_is_critical(self):
        # 300 units / ~87 a day = ~3.4 days of stock, but delivery takes 7 days
        self.assertEqual(run([product(on_hand=300, lead=7)], sales_for("SKU-1", 84)).loc["SKU-1", "status"],
                         bot.CRITICAL)

    def test_stock_on_the_way_counts(self):
        # Same 300 on hand, but 500 more already ordered: 800 covers ~9 days, longer than the 7-day delivery
        status = run([product(on_hand=300, on_order=500, lead=7)], sales_for("SKU-1", 84)).loc["SKU-1", "status"]
        self.assertNotEqual(status, bot.CRITICAL)

    def test_inside_safety_buffer_is_reorder(self):
        # Bumpy sales create safety stock. Stock that covers the delivery wait but has dipped
        # into the safety buffer should say REORDER, not CRITICAL.
        sales = sales_for("SKU-1", 84)
        weeks = (sales["date"] - sales["date"].min()).dt.days // 7
        sales.loc[weeks % 2 == 1, "units_sold"] *= 2  # every other week is twice as busy
        demand = bot.forecast_demand(sales).iloc[0]
        lead_demand = demand["forecast_daily_demand"] * 7
        df = run([product(on_hand=int(lead_demand) + 5, lead=7)], sales)
        self.assertGreater(df.loc["SKU-1", "safety_stock"], 5)
        self.assertEqual(df.loc["SKU-1", "status"], bot.REORDER)

    def test_healthy(self):
        self.assertEqual(run([product(on_hand=2000, lead=7)], sales_for("SKU-1", 84)).loc["SKU-1", "status"], bot.OK)

    def test_overstock(self):
        # 20,000 units is about 230 days of supply (threshold is 120)
        self.assertEqual(run([product(on_hand=20000)], sales_for("SKU-1", 84)).loc["SKU-1", "status"], bot.OVERSTOCK)


class OrderQuantityTests(unittest.TestCase):
    def test_order_is_whole_cases_and_restores_stock(self):
        df = run([product(on_hand=300, lead=7, case_pack=12)], sales_for("SKU-1", 84))
        row = df.loc["SKU-1"]
        self.assertGreater(row["suggested_order_qty"], 0)
        self.assertEqual(row["suggested_order_qty"] % 12, 0)
        # After the order arrives, stock is above the reorder point
        self.assertGreater(row["inventory_position"] + row["suggested_order_qty"], row["reorder_point"])

    def test_healthy_item_orders_nothing(self):
        self.assertEqual(run([product(on_hand=2000)], sales_for("SKU-1", 84)).loc["SKU-1", "suggested_order_qty"], 0)


class MessyDataTests(unittest.TestCase):
    """Real company data is never clean. None of these may crash the bot."""

    def test_new_product_with_no_sales_history(self):
        df = run([product("SKU-1"), product("SKU-NEW", on_hand=40)], sales_for("SKU-1", 84))
        self.assertEqual(df.loc["SKU-NEW", "status"], bot.NO_DATA)
        self.assertEqual(df.loc["SKU-NEW", "suggested_order_qty"], 0)

    def test_free_item_with_zero_cost(self):
        df = run([product(cost=0, on_hand=10)], sales_for("SKU-1", 84))
        self.assertEqual(df.loc["SKU-1", "eoq"], 0)
        self.assertGreater(df.loc["SKU-1", "suggested_order_qty"], 0)

    def test_one_day_of_history(self):
        run([product()], sales_for("SKU-1", 1))

    def test_item_that_never_sells(self):
        df = run([product(on_hand=50)], sales_for("SKU-1", 84, weekday_units=0, weekend_units=0))
        self.assertEqual(df.loc["SKU-1", "status"], bot.OVERSTOCK)

    def test_missing_days_count_as_zero_sales(self):
        sales = sales_for("SKU-1", 84)
        gappy = sales[sales["date"].dt.day_of_week < 5]  # weekend rows missing entirely
        f = bot.forecast_demand(gappy).iloc[0]
        self.assertAlmostEqual(f["forecast_daily_demand"], 5 * 100 / 7, places=6)


class BacktestTests(unittest.TestCase):
    def test_perfect_forecast_on_steady_sales(self):
        _, summary = bot.backtest(sales_for("SKU-1", 84), pd.DataFrame([product()]))
        self.assertAlmostEqual(summary["accuracy"], 1.0)
        self.assertAlmostEqual(summary["bias"], 0.0)

    def test_too_little_history_is_skipped(self):
        table, summary = bot.backtest(sales_for("SKU-1", 20), pd.DataFrame([product()]))
        self.assertIsNone(summary)


class EndToEndTest(unittest.TestCase):
    def test_full_report_is_written(self):
        """Runs the whole pipeline on the shipped sample data into a temporary folder."""
        products, sales = bot.load_data()
        original = bot.BASE_DIR
        with tempfile.TemporaryDirectory() as tmp:
            bot.BASE_DIR = Path(tmp)
            try:
                demand = bot.forecast_demand(sales)
                accuracy, summary = bot.backtest(sales, products)
                df = bot.analyze(products, demand)
                po = bot.build_purchase_orders(df)
                path = bot.write_excel_report(df, po, datetime.now(), accuracy, summary)
                bot.append_history(df, po, datetime.now(), summary)
                with pd.ExcelFile(path) as book:
                    sheets = book.sheet_names
                self.assertIn("Forecast Accuracy", sheets)
                self.assertIn("Purchase Orders", sheets)
                payloads = bot.build_discord_payloads(df, po, datetime.now(), path.name, summary)
                self.assertTrue(payloads)
            finally:
                bot.BASE_DIR = original


if __name__ == "__main__":
    unittest.main()
