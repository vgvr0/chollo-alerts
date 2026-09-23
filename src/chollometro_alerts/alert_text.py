"""Deterministic language hints for the merchant and schedule parts of an alert.

The sentence is interpreted by the model, but two of its parts are too
important to be left to a guess: which shops the alert allows or excludes, and
which hours may receive Telegram. Both are read here with plain, deterministic
rules and merged into whatever the provider answered, so a missing or
hallucinated answer can never silently change them.

The reader is deliberately conservative:

* a shop is only read where the sentence says so (`de Amazon o PcComponentes`,
  `solo Amazon`, `no AliExpress`, `excepto AliExpress`, `excluir AliExpress`)
  and it has to look like a shop: a known one, or a name with a shop-like shape
  (a dot, a digit or an inner capital, as in `PcComponentes`, `MediaMarkt`,
  `Amazon.es`). A product phrase such as `Zapatillas Nike` is never a shop.
* hours have to be concrete (`08:00`, `23:00`). A vague period ("por la
  noche") is reported as ambiguous instead of being turned into invented
  bounds: the product has no definition of "night", so the operator is asked
  for the exact hours.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .alert_rule import NotificationWindow as RuleNotificationWindow
from .merchants import normalize_merchant
from .schedule import (
    NotificationWindow,
    default_timezone,
    parse_time,
    validate_timezone,
)

# Well-known shops of the Spanish market. The list only has to make the
# unambiguous cases (`de Amazon`, `no Carrefour`) readable without the model;
# everything else keeps working through the shop-like shape or the model.
KNOWN_MERCHANTS = frozenset(
    normalize_merchant(name)
    for name in (
        "Amazon",
        "AliExpress",
        "PcComponentes",
        "MediaMarkt",
        "Carrefour",
        "El Corte Inglés",
        "Fnac",
        "Worten",
        "Miravia",
        "Lidl",
        "Aldi",
        "Alcampo",
        "Eroski",
        "Ikea",
        "Decathlon",
        "Sprinter",
        "Zalando",
        "Privalia",
        "Temu",
        "Shein",
        "eBay",
        "Wallapop",
        "Leroy Merlin",
        "Bricomart",
        "Coolmod",
        "Neobyte",
        "Mercadona",
        "Ahorramas",
        "Hipercor",
        "Deporvillage",
    )
)

_ALLOWED_CUES = (
    "de",
    "solo",
    "sólo",
    "únicamente",
    "unicamente",
    "exclusivamente",
    "en",
)
_EXCLUDED_CUES = frozenset(
    {"no", "excepto", "salvo", "excluir", "excluye", "excluyendo", "sin"}
)
_CUE_WORDS = tuple(sorted(_EXCLUDED_CUES | set(_ALLOWED_CUES) | {"tiendas", "tienda"}))
_CUE_RE = re.compile(rf"\b(?P<cue>{'|'.join(_CUE_WORDS)})\b", re.IGNORECASE)

# A word of a shop name: starts with a letter, keeps dots, digits and dashes.
_WORD_RE = re.compile(r"[^\W\d_][\w.\-']*", re.UNICODE)
_SEPARATORS = frozenset({"o", "y", "e", "u"})
_LIST_SEPARATOR_RE = re.compile(r"\s(?:o|y|e)\s", re.IGNORECASE)

_KNOWN_TIMEZONES = re.compile(r"\b([A-Za-z]+/[A-Za-z_]+)\b")
_TIMEZONE_ALIASES = {
    "hora de madrid": "Europe/Madrid",
    "hora peninsular": "Europe/Madrid",
    "hora espanola": "Europe/Madrid",
    "hora española": "Europe/Madrid",
}

_TIME = r"\d{1,2}:\d{2}"
_WINDOW_PATTERNS = (
    re.compile(rf"\bentre\s+(?:las\s+)?({_TIME})\s+y\s+(?:las\s+)?({_TIME})"),
    re.compile(rf"\bde\s+(?:las\s+)?({_TIME})\s+a\s+(?:las\s+)?({_TIME})"),
    re.compile(rf"\bdesde\s+(?:las\s+)?({_TIME})\s+hasta\s+(?:las\s+)?({_TIME})"),
    re.compile(rf"\b({_TIME})\s*(?:-|–|—|hasta|a)\s*({_TIME})"),
)

# Periods of the day with no definition in the product: they are never turned
# into hours by guesswork.
_VAGUE_PERIODS = re.compile(
    r"\bpor las?\s+(?:noches?|mañanas?|mananas?|tardes?|madrugadas?)\b"
    r"|\bde\s+madrugada\b",
    re.IGNORECASE,
)

AMBIGUITY_MESSAGE = (
    "no tengo una definición de ese horario: dime las horas exactas, por "
    "ejemplo «entre las 23:00 y las 07:00»"
)


@dataclass(frozen=True)
class MerchantMentions:
    """The shops a sentence allows and excludes, as they were written."""

    allowed: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.allowed and not self.excluded


def _inner_capital(word: str) -> bool:
    """`PcComponentes` yes, `ASUS` no: an acronym is not a camel-case name."""
    rest = word[1:]
    return (
        any(char.isupper() for char in rest)
        and not rest.isupper()
        and not word.isupper()
    )


def _looks_like_a_shop(name: str) -> bool:
    """A conservative shape test: never a plain product phrase."""
    text = name.strip()
    if not text:
        return False
    if normalize_merchant(text) in KNOWN_MERCHANTS:
        return True
    if "." in text or any(char.isdigit() for char in text):
        return True
    return len(text.split()) == 1 and _inner_capital(text)


def _is_list_word(word: str) -> bool:
    return word[0].isupper() or word.casefold() in _SEPARATORS


def _names_after(text: str, start: int) -> tuple[list[str], int]:
    """The shop names written right after a cue, and where they end."""
    names: list[str] = []
    current: list[str] = []
    end = start
    for match in _WORD_RE.finditer(text, start):
        word = match.group(0)
        if word.casefold() in _SEPARATORS and current:
            names.append(" ".join(current))
            current = []
            end = match.end()
            continue
        if word[0].isupper():
            current.append(word)
            end = match.end()
            continue
        break
    if current:
        names.append(" ".join(current))
    return names, end


def _bare_list(text: str) -> list[str]:
    """`Amazon y PcComponentes`: a list of shops written without any cue.

    Only accepted when *every* name of the list looks like a shop, which is
    what keeps a product list (`Cerveza Mahou y Coca-Cola`) out.
    """
    for separator in _LIST_SEPARATOR_RE.finditer(text):
        left, right = separator.start(), separator.end()
        for match in reversed(list(_WORD_RE.finditer(text, 0, separator.start()))):
            if match.end() != left or not _is_list_word(match.group(0)):
                break
            left = match.start()
        for match in _WORD_RE.finditer(text, right):
            if match.start() != right or not _is_list_word(match.group(0)):
                break
            right = match.end()
        names = _split_names(text[left:right])
        if len(names) >= 2 and all(_looks_like_a_shop(name) for name in names):
            return names
    return []


def _split_names(run: str) -> list[str]:
    """Split `Amazon o PcComponentes` into its names, dropping the separators."""
    names: list[str] = []
    current: list[str] = []
    for match in _WORD_RE.finditer(run):
        word = match.group(0)
        if word.casefold() in _SEPARATORS and current:
            names.append(" ".join(current))
            current = []
            continue
        current.append(word)
    if current:
        names.append(" ".join(current))
    return names


def _unique(values):
    seen: list[str] = []
    keys: set[str] = set()
    for value in values:
        key = normalize_merchant(value)
        if key and key not in keys:
            keys.add(key)
            seen.append(value)
    return tuple(seen)


def extract_merchant_mentions(text: str) -> MerchantMentions:
    """Read `de X`, `solo X`, `no Y`, `excepto Y`… from a sentence."""
    if not text:
        return MerchantMentions()
    allowed: list[str] = []
    excluded: list[str] = []
    consumed_until = 0
    for cue in _CUE_RE.finditer(text):
        if cue.start() < consumed_until:
            # The names of this cue were already read by the previous one.
            continue
        names, end = _names_after(text, cue.end())
        consumed_until = max(consumed_until, end)
        target = excluded if cue.group("cue").casefold() in _EXCLUDED_CUES else allowed
        target.extend(name for name in names if _looks_like_a_shop(name))
    allowed.extend(_bare_list(text))
    return MerchantMentions(_unique(allowed), _unique(excluded))


def extract_timezone(text: str) -> str | None:
    """The timezone the sentence names, if it names one at all."""
    if not text:
        return None
    folded = text.casefold()
    for alias, zone in _TIMEZONE_ALIASES.items():
        if alias in folded:
            return zone
    match = _KNOWN_TIMEZONES.search(text)
    if match is None:
        return None
    try:
        return validate_timezone(match.group(1))
    except ValueError:
        return None


def extract_notification_window(text: str) -> NotificationWindow | None:
    """The concrete window of a sentence (`08:00 - 23:00`), or None."""
    if not text:
        return None
    for pattern in _WINDOW_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        try:
            return NotificationWindow(
                parse_time(match.group(1)),
                parse_time(match.group(2)),
                extract_timezone(text) or default_timezone(),
            )
        except ValueError:  # pragma: no cover - the pattern only matches HH:MM
            continue
    return None


def vague_period(text: str) -> str | None:
    """The vague period a sentence mentions ("por la noche"), if any."""
    match = _VAGUE_PERIODS.search(text or "")
    return match.group(0).strip() if match else None


def ambiguity_error(text: str) -> str | None:
    """The clarification to ask when a vague period has no concrete hours."""
    period = vague_period(text)
    if period is None or extract_notification_window(text) is not None:
        return None
    return f"«{period}»: {AMBIGUITY_MESSAGE}"


def merge_intent(intent, text: str):
    """Fill the intent's merchant and schedule fields from the sentence.

    The deterministic reading wins when it exists: it comes from the literal
    text, so it cannot be an invention. The provider's answer is kept for
    everything this reader does not understand (a lowercase shop name, a
    sentence in another language).
    """
    error = ambiguity_error(text)
    if error:
        raise ValueError(error)
    mentions = extract_merchant_mentions(text)
    window = extract_notification_window(text)
    updates = {}
    if mentions.allowed:
        updates["include_merchants"] = list(mentions.allowed)
    if mentions.excluded:
        updates["exclude_merchants"] = list(mentions.excluded)
    if window is not None:
        updates["notify_window_start"] = f"{window.start:%H:%M}"
        updates["notify_window_end"] = f"{window.end:%H:%M}"
        updates["notify_timezone"] = window.timezone
    return intent.model_copy(update=updates) if updates else intent


def merge_rule(rule, text: str):
    """The same merge for the `alert parse`/`alert add` path (`AlertRule`)."""
    error = ambiguity_error(text)
    if error:
        raise ValueError(error)
    mentions = extract_merchant_mentions(text)
    window = extract_notification_window(text)
    updates = {}
    if mentions.allowed:
        updates["include_merchants"] = tuple(mentions.allowed)
    if mentions.excluded:
        updates["exclude_merchants"] = tuple(mentions.excluded)
    if window is not None:
        updates["notification_window"] = RuleNotificationWindow(
            start=window.start, end=window.end, timezone=window.timezone
        )
    return rule.model_copy(update=updates) if updates else rule
