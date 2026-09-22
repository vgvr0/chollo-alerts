# 🛒 Chollometro Alerts

Smart deal monitoring for [Chollometro](https://www.chollometro.com/) with configurable alert rules, automated product analysis and Telegram notifications.

The application continuously checks public Chollometro searches, detects **new deals**, extracts structured product information and evaluates them against configurable rules before deciding whether an alert should be sent.

It combines deterministic extraction with optional **LLM-powered analysis using DeepSeek**, while keeping pricing calculations and deal decisions deterministic and reproducible.

## ✨ Features

* 🔎 **Automated deal monitoring** from public Chollometro searches
* 🚨 **Configurable alert rules** for different products and searches
* 🆕 **New-deal detection** using persistent SQLite state
* 🧠 **Hybrid product extraction**

  * deterministic parsing when possible
  * DeepSeek LLM fallback for ambiguous product information
* 📦 Structured extraction of product attributes such as quantity and total volume
* 💰 Deterministic **price-per-unit analysis**
* 🎯 Rule-based filtering before sending notifications
* 📲 **Telegram notifications** for matching new deals
* 💾 Persistent extraction cache to avoid unnecessary LLM calls
* 🔁 Safe retry behaviour for failed notifications
* 📊 Runtime metrics for LLM usage, cache hits and detected deals
* 🧪 Dry-run and baseline modes for safe testing
* 🔐 Environment-based secret management

## 🏗️ How it works

```text
Chollometro Search
        │
        ▼
   HTML Parser
        │
        ▼
  New Deal Detection
        │
        ▼
Product Extraction
   │           │
   │           └──► DeepSeek LLM
   │                (when needed)
   ▼
Deterministic Extraction
        │
        ▼
   Pricing Engine
        │
        ▼
   Interest Rules
        │
        ▼
   SQLite State
        │
        ▼
 Telegram Alert
```

The LLM is intentionally limited to **extracting structured facts from the deal**. It does not decide whether a product is a good deal or calculate prices.

This separation keeps the decision pipeline deterministic, testable and easier to extend.

## 🚀 Example

You can define a search such as:

```text
"cerveza"
```

The application establishes the current results as a baseline. On subsequent executions, only newly discovered deals are evaluated.

For each new deal:

```text
New Chollometro deal
        ↓
Extract product information
        ↓
Calculate comparable price
        ↓
Evaluate configured rule
        ↓
Match?
   ├── No  → Store and ignore
   └── Yes → Send Telegram alert
```

This makes it possible to monitor products continuously without receiving notifications for deals that already existed when the alert was created.

## 🔁 Deterministic rule evaluation

`AlertRule.constraints` supports `max_price`, `max_price_per_liter`,
`max_price_per_unit`, `min_quantity`, `min_volume_l` and `min_temperature`, and
every constraint present in the rule must hold. `PricingEngine` derives the
comparable prices (`price_per_liter`, `price_per_unit`) from the extracted facts,
so `InterestEngine` only compares numbers; when the required fact is unknown (for
example the quantity of a `max_price_per_unit` rule) the deal is rejected as
`REJECTED_UNKNOWN_QUANTITY` instead of assuming one unit. The `max_price*` limits
are exclusive: the price must be strictly below the configured value.

Production scans, `run-rules --dry-run` and `alert test` share the same path
(`repository.rule_from_row()` → `AlertRule` → `InterestRule` → `PricingEngine` →
`InterestEngine`) and differ only in where the deals come from, which side effects
are allowed and how the verdict is presented.

## 🧪 Testing an alert against historical deals

`alert test` replays a persisted rule against the deals already stored locally, so
a rule can be validated without waiting for new deals:

```powershell
chollometro-alerts alert test 12
chollometro-alerts alert test 12 --limit 100
```

Example output:

```text
RULE #12

Query: zapatillas
Product: zapatillas
Brand: ASICS
max_price: 80

Historical deals available: 87
Deals evaluated: 53

MATCH:        6
REJECT:       43
NOT_EVALUABLE: 4
```

`alert test` is a read-only simulator:

* it uses deals already known locally (`--limit` caps how many are evaluated,
  default 200; `--limit 0` evaluates all of them);
* it never scrapes: no new requests to Chollometro;
* it never calls the LLM: it reuses the cached `ProductExtraction` and, when there
  is none, only the deterministic extraction of the stored text;
* it never sends Telegram;
* it never changes state: no deals, extractions, observations, baseline,
  `notified_at`, matches or `scan_runs` are created or modified.

The report distinguishes:

* `MATCH`: the rule would have alerted for that deal;
* `REJECT`: the deal is rejected with the existing reason (`REJECTED_PRICE`,
  `REJECTED_PRICE_PER_UNIT`, `REJECTED_BRAND`, `REJECTED_QUANTITY`, ...);
* `NOT_EVALUABLE`: local data is not enough to check the rule (for example an
  unknown quantity or volume, `REJECTED_UNKNOWN_QUANTITY`). Production semantics
  do not change; only the replay report labels it this way.

When `Historical deals available` is 0 the replay says nothing about the rule:
there were no local deals to replay yet, so **0 matches does not mean the alert is
broken**.
