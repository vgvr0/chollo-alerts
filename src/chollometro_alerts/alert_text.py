"""Deterministic language hints for the merchant and schedule parts of an alert.

The sentence is interpreted by the model, but two of its parts are too
important to be left to a guess: which shops the alert allows or excludes, and
which hours may receive Telegram. Both are read here with plain, deterministic
rules and merged into whatever the provider answered, so a missing or
hallucinated answer can never silently change them. The Chollometro
temperature window ("más de 500 grados", "menos de 100°") is read the same way.

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
* a temperature is only read when the number carries a temperature unit
  (`500 grados`, `500°`). A price (`por menos de 700 €`) is never turned into
  a temperature, and a negated ceiling ("no quiero chollos por debajo de 100
  grados") is read as the floor it really states.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .alert_rule import AlertConstraints
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


# --- Chollometro temperature ------------------------------------------------ #
#
# The degrees of a deal are a provider fact; the sentence only says which
# values the operator wants. The number is therefore only read when it carries
# a temperature unit, which is what keeps a price ("por menos de 700 €") from
# ever becoming a temperature.

_TEMPERATURE_NUMBER = r"\d+(?:[.,]\d+)?"
# `500°`, `500 °C`, `500 grados`, `500 grados de temperatura`.
_TEMPERATURE_UNIT = r"(?:°\s*[cf]?|grados?(?:\s+de\s+temperatura)?)"

_TEMPERATURE_RANGE_PATTERNS = (
    re.compile(
        rf"\bentre\s+(?:los\s+)?({_TEMPERATURE_NUMBER})\s*(?:{_TEMPERATURE_UNIT})?"
        rf"\s*y\s+(?:los\s+)?({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bde\s+({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}"
        rf"\s+a\s+(?:los\s+)?({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bdesde\s+({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}"
        rf"\s+hasta\s+(?:los\s+)?({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}",
        re.IGNORECASE,
    ),
)

_TEMPERATURE_MIN_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        rf"\b(?:m[áa]s|mas)\s+de\s+(?:los\s+)?({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}",
        rf"\bal\s+menos\s+(?:de\s+)?({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}",
        rf"\bcomo\s+m[íi]nimo\s+(?:de\s+)?({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}",
        rf"\bpor\s+lo\s+menos\s+(?:de\s+)?({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}",
        rf"\bm[íi]nimo\s+(?:de\s+)?({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}",
        (
            rf"\bsuper(?:a|an|e|en|ar|ior(?:es)?\s+a)\s+(?:los\s+|las\s+)?"
            rf"({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}"
        ),
        rf"\bpor\s+encima\s+de\s+(?:los\s+)?({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}",
        rf"\bmayor(?:es)?\s+(?:que|a|de)\s+(?:los\s+)?({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}",
    )
)

_TEMPERATURE_MAX_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        rf"\bno\s+m[áa]s\s+de\s+(?:los\s+)?({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}",
        rf"\bmenos\s+de\s+(?:los\s+)?({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}",
        rf"\bpor\s+menos\s+de\s+(?:los\s+)?({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}",
        rf"\bcomo\s+m[áa]ximo\s+(?:de\s+)?({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}",
        rf"\bm[áa]ximo\s+(?:de\s+)?({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}",
        rf"\bhasta\s+(?:los\s+)?({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}",
        rf"\binferior(?:es)?\s+a\s+(?:los\s+)?({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}",
        rf"\bpor\s+debajo\s+de\s+(?:los\s+)?({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}",
        rf"\bmenor(?:es)?\s+(?:que|a|de)\s+(?:los\s+)?({_TEMPERATURE_NUMBER})\s*{_TEMPERATURE_UNIT}",
    )
)

# Words that turn a ceiling into the floor it really states: "no quiero
# chollos por debajo de 100 grados" is a minimum of 100 degrees.
_TEMPERATURE_NEGATIONS = re.compile(
    r"\b(?:no|nunca|sin|evita|evitar|nada\s+de)\b", re.IGNORECASE
)
# A cue is read as the floor it states unless the words right before it negate
# it: "no más de 250 grados" is a ceiling written backwards.
_TEMPERATURE_NEGATED_TAIL = re.compile(r"\b(?:no|sin|nunca|ni)\s*$", re.IGNORECASE)
_CLAUSE_BOUNDARIES = ".;\n"


@dataclass(frozen=True)
class TemperatureMentions:
    """The temperature window a sentence states, in degrees."""

    minimum: float | None = None
    maximum: float | None = None

    @property
    def empty(self) -> bool:
        return self.minimum is None and self.maximum is None


def _degrees(raw: str) -> float:
    """`500` stays whole, `500,5` keeps its decimals."""
    value = float(raw.replace(",", "."))
    return int(value) if value.is_integer() else value


def _first_outside(patterns, text: str, spans, accept=None) -> re.Match | None:
    """First match of the highest-priority pattern, outside the given spans."""
    for pattern in patterns:
        for match in pattern.finditer(text):
            if any(start <= match.start() < end for start, end in spans):
                continue
            if accept is not None and not accept(match):
                continue
            return match
    return None


def _negated_before(text: str, start: int) -> bool:
    """True when the words right before `start` negate the cue."""
    return _TEMPERATURE_NEGATED_TAIL.search(text[:start]) is not None


def _negates_the_ceiling(text: str, match: re.Match) -> bool:
    """True when a negation word before the cue flips it into a minimum."""
    if match.group(0).casefold().startswith("no "):
        # "no más de 300 grados" is a ceiling, not a negated floor.
        return False
    clause_start = 0
    for boundary in _CLAUSE_BOUNDARIES:
        clause_start = max(clause_start, text.rfind(boundary, 0, match.start()) + 1)
    return _TEMPERATURE_NEGATIONS.search(text, clause_start, match.start()) is not None


def extract_temperature_mentions(text: str) -> TemperatureMentions:
    """Read the temperature window of a sentence ("más de 500 grados")."""
    if not text:
        return TemperatureMentions()
    minimum = maximum = None
    consumed: list[tuple[int, int]] = []
    for pattern in _TEMPERATURE_RANGE_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        # The bounds are taken in the order they were written: a reversed range
        # ("entre 500 y 100 grados") is a contradiction, and the rule model
        # answers it with a clarification instead of a silent swap.
        minimum, maximum = _degrees(match.group(1)), _degrees(match.group(2))
        consumed.append(match.span())
        break
    lower = _first_outside(
        _TEMPERATURE_MIN_PATTERNS,
        text,
        consumed,
        accept=lambda match: not _negated_before(text, match.start()),
    )
    if lower is not None and minimum is None:
        minimum = _degrees(lower.group(1))
        consumed.append(lower.span())
    upper = _first_outside(_TEMPERATURE_MAX_PATTERNS, text, consumed)
    if upper is not None:
        value = _degrees(upper.group(1))
        if _negates_the_ceiling(text, upper):
            # "no ... por debajo de 100 grados": the deal must reach 100.
            if minimum is None:
                minimum = value
        elif maximum is None:
            maximum = value
    return TemperatureMentions(minimum, maximum)


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
    temperature = extract_temperature_mentions(text)
    updates: dict[str, object] = {}
    if mentions.allowed:
        updates["include_merchants"] = list(mentions.allowed)
    if mentions.excluded:
        updates["exclude_merchants"] = list(mentions.excluded)
    if temperature.minimum is not None:
        updates["temperature_min"] = temperature.minimum
    if temperature.maximum is not None:
        updates["temperature_max"] = temperature.maximum
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
    temperature = extract_temperature_mentions(text)
    updates: dict[str, object] = {}
    if mentions.allowed:
        updates["include_merchants"] = tuple(mentions.allowed)
    if mentions.excluded:
        updates["exclude_merchants"] = tuple(mentions.excluded)
    if not temperature.empty:
        # The sentence states the temperature window; it is merged through the
        # model validator so a contradictory pair is a clarification, never a
        # rule that silently matches nothing.
        constraints = AlertConstraints.model_validate(
            {
                **rule.constraints.model_dump(),
                "temperature_min": temperature.minimum
                if temperature.minimum is not None
                else rule.constraints.temperature_min,
                "temperature_max": temperature.maximum
                if temperature.maximum is not None
                else rule.constraints.temperature_max,
            }
        )
        updates["constraints"] = constraints
    if window is not None:
        updates["notification_window"] = RuleNotificationWindow(
            start=window.start, end=window.end, timezone=window.timezone
        )
    return rule.model_copy(update=updates) if updates else rule
