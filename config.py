"""
Settings for the Inventory Reorder Bot.
Change these numbers to see how the results change. That's a great way to learn!
"""

# --- Discord ---------------------------------------------------------------
# Do NOT paste your webhook URL here. Put it in a file named .env like this:
#     DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/....
# (See README step 4.) Keeping it out of the code means it never ends up on GitHub.

# --- Input files -----------------------------------------------------------
PRODUCTS_FILE = "data/products.csv"
SALES_HISTORY_FILE = "data/sales_history.csv"
REPORTS_FOLDER = "reports"

# --- Inventory math --------------------------------------------------------
# Service level = how often you want to avoid running out during a delivery wait.
# 0.95 means "95% of the time we should NOT run out". Higher = more safety stock.
SERVICE_LEVEL = 0.95

# Forecast smoothing (0 to 1), applied to WEEKLY sales totals.
# Higher = the forecast reacts faster to recent weeks. 0.2 to 0.4 is a common range.
# (Weekly totals are used so slow weekends don't drag the forecast down
#  depending on which day of the week you run the bot.)
SMOOTHING_ALPHA = 0.3

# An item needs at least this many full weeks of sales to use the smoothed forecast.
# With less, the bot uses a plain average and says so in the report.
MIN_WEEKS_FOR_FORECAST = 2

# Forecast accuracy check: hide this many recent weeks, forecast them, compare to what sold.
BACKTEST_WEEKS = 4

# Cost to place one purchase order (paperwork, shipping setup, people's time).
ORDERING_COST_PER_ORDER = 50.00

# Yearly cost of holding inventory, as a % of the item's cost
# (storage, insurance, money tied up). 25% is a common rule of thumb.
ANNUAL_HOLDING_COST_RATE = 0.25

# ABC analysis cutoffs (share of total yearly spend)
ABC_A_CUTOFF = 0.80  # top items that make up 80% of spend -> "A" (watch closely)
ABC_B_CUTOFF = 0.95  # next 15% -> "B"; everything else -> "C"

# Flag items with more than this many days of stock as "overstock"
OVERSTOCK_DAYS = 120
