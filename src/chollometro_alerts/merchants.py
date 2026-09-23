"""Deterministic merchant filtering for alert rules.

`allowed_merchants` (stored as `include_merchants`) and `excluded_merchants`
(stored as `exclude_merchants`) are decided locally, never by the model:

* an empty allow list means "any shop except the excluded ones";
* a non-empty allow list means "only these shops";
* the exclusion list always wins, so a shop that appears in both lists is
  rejected.

The comparison is a **normalised equality**, not a substring search. Case,
surrounding spaces, internal whitespace runs, accents/diacritics and
punctuation are folded away, so `pc componentes`, `PcComponentes` and
`PC COMPONENTES` are the same shop while `Amazon Marketplace XYZ` is *not*
`Amazon` and `Amazon.de` is not `Amazon` either. That is deliberately
conservative: a wrong "this is your shop" would notify the operator about a
shop the alert never asked for.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# The two ways a merchant can reject a deal. The exclusion list keeps the
# original `REJECTED_MERCHANT` reason it has always used.
MERCHANT_EXCLUDED = "REJECTED_MERCHANT"
MERCHANT_NOT_ALLOWED = "REJECTED_MERCHANT_NOT_ALLOWED"

_WORD_SEPARATORS = re.compile(r"[^\w]+", re.UNICODE)


def normalize_merchant(value) -> str:
    """The comparison key of a merchant name: robust, and never fuzzy.

    Accents are folded (`Showroomprive` == `Showroomprivé`), case and dashes
    are ignored and the spaces between words are dropped, which covers the
    reasonable differences between the two providers (GraphQL
    `merchant.merchantName` and the HTML card's `data-t="merchantLink"` text)
    and between the two ways an operator writes a shop: `Pc Componentes` and
    `PcComponentes` are the same shop.

    Nothing else is normalised: word order is kept and no word is ever dropped,
    so a longer shop name can never match a shorter one (`Amazon Marketplace
    XYZ` is not `Amazon`).
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_form = decomposed.encode("ascii", "ignore").decode("ascii")
    words = _WORD_SEPARATORS.split(ascii_form.casefold())
    return "".join(word for word in words if word)


def merchant_keys(values) -> frozenset[str]:
    """The normalised keys of a configured merchant list (blank entries dropped)."""
    if not values:
        return frozenset()
    if isinstance(values, str):
        values = (values,)
    return frozenset(
        key for key in (normalize_merchant(value) for value in values) if key
    )


def _configured(values, key):
    """The configured spelling that produced `key`, for logs and messages."""
    for value in values or ():
        if normalize_merchant(value) == key:
            return str(value).strip()
    return None


@dataclass(frozen=True)
class MerchantVerdict:
    """The outcome of the deterministic merchant filter for one deal."""

    accepted: bool
    reason: str | None = None
    merchant: str | None = None
    matched: str | None = None
    unknown: bool = False

    @classmethod
    def allowed(cls, merchant):
        return cls(True, None, merchant)


def merchant_verdict(merchant, allowed=(), excluded=()) -> MerchantVerdict:
    """Decide one deal's merchant against the rule's two lists.

    A deal without a usable merchant name can never prove it belongs to the
    allow list, so it is rejected when the rule has one (the deal is not
    silently treated as "any shop"). With only an exclusion list it passes,
    exactly like the previous behaviour: an unknown name cannot prove an
    exclusion either.
    """
    key = normalize_merchant(merchant)
    excluded_keys = merchant_keys(excluded)
    allowed_keys = merchant_keys(allowed)
    if key and key in excluded_keys:
        return MerchantVerdict(
            False,
            MERCHANT_EXCLUDED,
            merchant,
            matched=_configured(excluded, key),
        )
    if allowed_keys and key not in allowed_keys:
        return MerchantVerdict(
            False,
            MERCHANT_NOT_ALLOWED,
            merchant,
            matched=None,
            unknown=not key,
        )
    return MerchantVerdict.allowed(merchant)
