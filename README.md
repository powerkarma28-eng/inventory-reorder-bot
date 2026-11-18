# 📦 Inventory Reorder Bot

A Python bot that monitors inventory, forecasts demand, and tells a procurement team **what to reorder, how much, and from which supplier**. It sends alerts to **Discord** and saves a formatted **Excel report** with a running history for trend analysis.

## What it does

1. **Forecasts demand** from sales history using exponential smoothing on weekly totals (recent weeks count more, and the weekday you run it doesn't skew the result).
2. **Tests its own forecast** every run: hides the last 4 weeks, predicts them, and reports accuracy (WAPE) against the old daily method and a naive "same as last week" guess.
3. **Calculates safety stock** at a 95% service level, based on how much each item's forecast tends to miss and how long its supplier takes to deliver.
4. **Calculates the reorder point** for each item: expected sales during the delivery wait + safety stock.
5. **Flags each item** as 🔴 Out of Stock, 🟠 Critical (runs out before a new order could arrive), 🟡 Reorder, 🟢 OK, 🔵 Overstock, or ⚪ No Sales Data (new item).
6. **Recommends order quantities** with an order-up-to rule (reorder point + **EOQ**), rounded up to the supplier's case pack.
7. **Ranks items with ABC analysis** (A = the few items that make up ~80% of spend).
8. **Groups orders into purchase orders by supplier**, with totals and expected delivery dates.
9. **Sends a Discord alert** with one card per supplier PO.
10. **Writes an Excel report** (Summary with charts, Reorder Alerts, Purchase Orders, PO Line Items, All Items, Forecast Accuracy, Settings).
11. **Logs every run** to `reports/alert_history.xlsx` so you can analyze trends over time.
12. **Handles messy data**: new products with no history, $0 items, short histories and missing days don't crash it.

## Tests
```bash
python -m unittest -v test_bot
```
19 automated tests check each status, the order quantities, the forecast and the messy-data cases, and run the full report end to end.

An example report is in [`examples/sample_report.xlsx`](examples/sample_report.xlsx).

---

## Setup (step by step)

### 1. Install Python
Download Python 3.10 or newer from [python.org](https://www.python.org/downloads/).
On Windows, **check the box "Add Python to PATH"** during install.

### 2. Open a terminal in this folder
- **Windows:** open the folder in File Explorer, click the address bar, type `cmd`, press Enter.
- **Mac:** right-click the folder → *New Terminal at Folder*.

### 3. Install the packages the bot needs
```bash
pip install -r requirements.txt
```
(If `pip` doesn't work, try `pip3` or `python -m pip`.)

### 4. Connect Discord (optional — the bot works without it)
1. In your Discord server, open **Server Settings → Integrations → Webhooks → New Webhook**.
2. Pick the channel for alerts, then click **Copy Webhook URL**.
3. Make a copy of `.env.example` and name the copy `.env`.
4. Open `.env` and paste your URL after `DISCORD_WEBHOOK_URL=`.

> ⚠️ Treat the webhook URL like a password. `.env` is listed in `.gitignore`, so it won't be uploaded to GitHub.

### 5. Create sample data
```bash
python generate_sample_data.py
```
This creates `data/products.csv` and `data/sales_history.csv`. Run it again anytime for a new random scenario.

### 6. Run the bot
```bash
python bot.py
```
To test without sending to Discord (it prints a preview instead):
```bash
python bot.py --no-discord
```
Your report appears in the `reports/` folder.

---

## Using your own data

Replace the two CSV files in `data/` with your own, keeping the same column names:

**products.csv**
| Column | Meaning |
|---|---|
| sku | Product ID |
| product_name | Product name |
| category | Product group |
| supplier | Who you buy it from |
| unit_cost | Cost per unit ($) |
| on_hand | Units in stock right now |
| on_order | Units ordered but not delivered yet |
| lead_time_days | Days between ordering and delivery |
| case_pack | Units per case (minimum order multiple) |

**sales_history.csv**: one row per product per day, with columns `date`, `sku` and `units_sold`.

## Changing the settings

Open `config.py` to adjust the service level, the ordering and holding costs, the ABC cutoffs and the overstock threshold. Then rerun the bot and compare reports to see how the recommendations change.

## Running it automatically every day
- **Windows:** Task Scheduler → Create Basic Task → Action: *Start a program* → `python`, arguments `bot.py`, "Start in" = this folder.
- **Mac/Linux:** add a cron job, for example `0 8 * * 1-5 cd /path/to/inventory-reorder-bot && python3 bot.py` (8 AM on weekdays).

## Project structure
```
inventory-reorder-bot/
├── bot.py                    # main bot: forecasting, inventory math, Excel, Discord
├── config.py                 # settings you can change
├── generate_sample_data.py   # creates realistic test data
├── test_bot.py               # automated tests
├── data/                     # input CSVs
├── reports/                  # Excel reports + alert_history.xlsx (created when you run it)
├── examples/                 # sample output report
├── HOW_IT_WORKS.md           # plain-English explanation of the inventory logic
└── requirements.txt
```

## Tech used
Python · pandas · NumPy · openpyxl (Excel automation and charts) · Discord webhooks (REST API)
