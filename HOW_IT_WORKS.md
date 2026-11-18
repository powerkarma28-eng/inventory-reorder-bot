# How It Works

A plain-English explanation of the inventory logic behind the bot.

---
## The key ideas (what each one means)

### 1. Demand forecast
**Question:** How many of this item will we sell per day?
**How:** Exponential smoothing on **weekly** sales totals. It averages past weeks, but recent weeks count more than older ones. The weekly forecast is divided by 7 to get a daily rate.
**Why not a plain average?** If sales are trending up, a plain 90-day average would underestimate demand.
**Why weeks instead of days?** Weekends sell less. My first version smoothed daily numbers, so the forecast dropped whenever the data ended on a weekend. The same item forecast 284 a day on a Sunday and 369 a day on a Wednesday. Every full week contains exactly one weekend, so weekly totals remove that swing.

### 2. Lead time
**Question:** How many days after we order until the supplier delivers?
During that wait we keep selling, so we need enough stock to last until the delivery arrives.

### 3. Safety stock
**Question:** How much extra should we keep in case sales spike or a delivery is late?
**Formula:** `Z × (forecast error std. dev.) × √(lead time)`
- **Z** comes from the service level. 95% → Z ≈ 1.65. That means "we accept running out about 5% of the time during a delivery wait."
- The spread used is **how wrong the forecast tends to be**, not how much sales bounce around. A product that sells a lot more on weekdays is still predictable, so it shouldn't get extra buffer for that. Safety stock exists to cover forecast misses.
- Items whose forecasts miss more, or with **long** lead times, get more safety stock.

### 4. Reorder point (ROP)
**Question:** At what stock level should we place an order?
**Formula:** `(forecast daily demand × lead time) + safety stock`
Example: we sell 10/day, delivery takes 7 days, and safety stock is 15. ROP = 70 + 15 = **85**. When stock reaches 85, we order.

### 5. Inventory position
`on hand + on order`. We count stock that's already on its way, so we don't order the same thing twice.

### 6. EOQ (Economic Order Quantity)
**Question:** How much should we order each time?
- Ordering small amounts often means lots of paperwork and shipping costs.
- Ordering huge amounts means paying to store it and tying up cash.
- EOQ is the order size that balances those two costs. **Formula:** `√(2 × annual demand × cost per order ÷ yearly holding cost per unit)`
- **How much the bot actually orders ("order up to" rule):** it brings stock up to the reorder point plus one EOQ: `order = (reorder point + EOQ) − inventory position`. An item at its reorder point gets exactly one EOQ. An item that is out of stock gets more, to make up the gap.
- The bot then rounds up to full **case packs**, because suppliers sell in cases.

### 6b. Forecast accuracy (backtest)
**Question:** How do we know the forecast is any good?
**How:** Every run, the bot hides the last 4 weeks of sales, forecasts them from the weeks before, and compares the forecast with what actually sold.
**Measure:** WAPE (weighted absolute percentage error) = total units the forecast missed by ÷ total units actually sold. Accuracy = 1 − WAPE. WAPE is used instead of MAPE because MAPE breaks on slow sellers that sell 0 in some weeks.
**Benchmarks:** the score is compared with (a) the bot's first, daily method and (b) a naive guess, "next week = last week." A forecast that can't beat the naive guess isn't adding value.
**Bias** shows whether the forecast tends to run high (+) or low (−). Steady under-forecasting causes stockouts.

### 7. ABC analysis
Ranks items by yearly spend:
- **A items:** the few items that make up ~80% of spend. Watch these closely and negotiate prices on them.
- **B items:** the next ~15%.
- **C items:** the many cheap items. Keep the process simple for these.

### 8. Statuses
| Status | Meaning |
|---|---|
| 🔴 OUT OF STOCK | Zero units on hand |
| 🟠 CRITICAL | It will run out before a new order could arrive (stock + on order covers fewer days than the lead time) |
| 🟡 REORDER | At or below the reorder point: it covers the delivery wait, but it's into the safety buffer, so order now |
| 🟢 OK | Healthy |
| 🔵 OVERSTOCK | More than 120 days of supply; cash is tied up in inventory |
| ⚪ NO SALES DATA | A new item with no sales history yet; a person sets its first order by hand |

---

## Try this to learn more
1. In `config.py`, change `SERVICE_LEVEL` from `0.95` to `0.99`. Run the bot. What happens to safety stock and total spend?
2. Change `ORDERING_COST_PER_ORDER` to `200`. How do order sizes change? (They should get bigger.)
3. In `data/products.csv`, double one supplier's lead times. Which items become urgent?

