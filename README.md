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
