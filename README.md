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

## 🛡️ Chollometro failure handling

**ESCANEO CORRECTO + 0 RESULTADOS ≠ FALLO DE CHOLLOMETRO.** An empty but
complete search is `SUCCESS` with 0 deals; a search that could not be completed is
`FAILED`. Both states are distinguished in the scanner, in `scan_runs.status`, in
the logs, in the metrics (`SCAN_STATUS`, `SCAN_ERROR_TYPE`) and in the CLI
(`SCAN_STATUS=FAILED` and exit code 1 for `check` / `baseline`).

* **Timeout**: every request carries an explicit timeout
  (`CHOLLOMETRO_TIMEOUT_SECONDS`, default 20 s), so nothing waits forever.
* **Retries and backoff**: transient failures are retried up to
  `CHOLLOMETRO_MAX_RETRIES` times (default 2) with exponential backoff starting at
  `CHOLLOMETRO_RETRY_BACKOFF_SECONDS` (0.5 s) and capped at
  `CHOLLOMETRO_MAX_RETRY_BACKOFF_SECONDS` (30 s). The budget is bounded: no
  infinite loops, and the sleep is injectable so tests never wait.
* **429**: transient, and a numeric `Retry-After` is honoured within the backoff
  cap.
* **5xx** (`500`, `502`, `503`, `504`, plus `408`): transient, retried within the
  same budget.
* **4xx** (`400`, `401`, `403`, `404`): permanent, never retried, the scan fails.
* **Parse failures**: a 200 response that is not a recognisable Chollometro
  results page (captcha, error page, changed markup, empty body) raises a
  `ChollometroParseError`. An empty result set must say so in the page itself; it
  is never assumed from a missing payload.
* **Total failure**: no page was read, so nothing is evaluated, persisted,
  matched or notified, the previous baseline is untouched and no LLM call is
  spent. `scan_runs.error_type` records `TIMEOUT`, `NETWORK_ERROR`, `HTTP_503`,
  `PARSE_ERROR`, ...
* **Partial failure**: when page 1 answers and page 2 fails, the deals of page 1
  are kept (observations are claimed per deal, so the missing pages are simply
  discovered in a later cycle) but the scan is recorded as `PARTIAL`, never as a
  complete success. A rule whose **baseline** is being taken is stricter: an
  incomplete baseline fails closed (`INITIALIZING_FAILED`, rule disabled), because
  a partial baseline would silently hide deals.
* **Daemon recovery**: a provider failure is logged, recorded as `FAILED` and the
  daemon waits for the next cycle instead of exiting. Retries inside one scan
  produce at most one operational Telegram alert per logical failure (existing
  60-minute cooldown per error type), so a `503` never becomes alert spam, and no
  "0 chollos encontrados" message is ever sent for a failure.

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

### Product matching

The alert `product` is compared with the extracted `product_type` word by word,
after normalising case, accents, punctuation and a simple Spanish plural
(`zapatilla` matches `zapatillas`). The extracted type may carry qualifiers
*after* the product, so `zapatillas` matches `Zapatillas running asfalto` and
`mini pc` matches `Mini PC NAS`.

This is still a deterministic comparison, not semantic matching: `zapatillas`
never matches `running shoes`, and a trailing qualifier on its own is not a
match (`leche` does not match `chocolate con leche`). When the product fact is
missing the verdict is unchanged, so the replay keeps reporting it as
`NOT_EVALUABLE` instead of a wrong rejection.

### Creating and correcting alerts

Rules are created, updated and removed from Telegram using plain language, and
the price dimension follows the sentence:

```text
Avísame de zapatillas ASICS por menos de 200 €        → max_price = 200
Avísame de Coca-Cola por menos de 0,50 € por unidad   → max_price_per_unit = 0.50
Avísame de leche por menos de 0,79 € por litro        → max_price_per_liter = 0.79
Cambia la alerta 2 para avisarme de zapatillas ASICS por menos de 200 €
```

An update finds the existing rule by its `query` and `brand` (the numeric id in
the message is only the operator's reference), rewrites the `max_price` /
`price_unit` columns, replaces the structured rule and keeps the product and
brand that were already stored. After that, `alert test <id>` replays the
corrected price semantics. The CLI equivalents for a *new* rule are
`chollometro-alerts alert parse "<texto>"` and
`chollometro-alerts alert add "<texto>"`.

The same correction can be applied from Python through the canonical boundary,
without touching the legacy columns by hand:

```python
from chollometro_alerts.intent import AlertIntent, intent_to_rule
from chollometro_alerts.repository import DealRepository

repository = DealRepository("deals.sqlite3")
intent = AlertIntent(
    action="update",
    query="zapatillas",
    product_type="zapatillas",
    brand="ASICS",
    max_price=200,
    price_unit="absolute",
)
repository.apply_alert_intent(intent)
repository.attach_alert_rule(2, intent_to_rule(intent), "texto original")
```

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
