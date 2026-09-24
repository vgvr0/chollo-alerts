"""Explicit intent router for the alert-management messages of Telegram.

Creating an alert is an extraction problem: the sentence has to become a
structured `InterestRule`. Managing the alerts that already exist is a
different problem, and not every management sentence names a product:

    "Elimina lo de cualquier aviso por menos de 200 €"
    "Quita el límite de 200 € de la alerta de cascos"
    "Qué alertas tengo"

Those sentences used to reach the extractor like any other message, so a
clear deletion was answered with "Falta el producto o la marca" and an update
depended on the text matching one stored rule exactly. This module decides
*what the user is asking for* before anything is extracted, and reads the
reference the sentence makes to a stored alert: an ordinal ("la alerta 2"), a
price ("por menos de 200 €"), a shop ("de Amazon"), a deictic ("esa alerta") or
free text ("cascos").

Everything here is deterministic and offline: the router never calls the
provider, so deleting or updating an alert can never depend on a model's
guess. Only the operations that really create an alert (`CREATE_ALERT`) and the
sentences this router does not recognize (`UNKNOWN`) keep the original
extraction path.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from .alert_rule import AlertRule, NotificationWindow
from .alert_text import extract_merchant_mentions, extract_notification_window

AlertOperation = Literal[
    "CREATE_ALERT", "DELETE_ALERT", "UPDATE_ALERT", "LIST_ALERTS", "UNKNOWN"
]

# The `AlertIntent.action` each operation maps to, for the surfaces that still
# speak in the legacy vocabulary.
OPERATION_ACTIONS = {
    "CREATE_ALERT": "create",
    "DELETE_ALERT": "delete",
    "UPDATE_ALERT": "update",
    "LIST_ALERTS": "list",
}


def fold(text: str) -> str:
    """The canonical comparison form: lowercase, unaccented, single-spaced."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(plain.casefold().split())


# --- What a sentence is about ----------------------------------------------- #


def _word_set(block: str) -> frozenset[str]:
    """The words of a block, one per line: a long list of quoted words does not."""
    return frozenset(block.split())


# Words that carry no identity at all: articles, prepositions, comparators, the
# currency and the management verbs themselves. They are the filler of the
# sentence and must never take part in the similarity with a stored alert.
_FILLER_WORDS = _word_set(
    """
    a al algo algun alguna algunas algunos almenos antes aqui asi aunque bajo
    cada casi como con contra cual cuales cuando cuanto de del desde donde dos
    debajo el ella ellas ellos en encima entre era eres es esa esas ese eso esos
    esta
    estan estas este esto estos favor fue gracias hasta hay la las le les lo
    los mas me menos mi mis mucha mucho muy nada ni no nos nuestra nuestro o os
    otra otro para pero poco por porque que quien se segun si sin sobre solo son
    su sus te tengo ti toda todas todo todos tu tus un una uno unos y ya
    aproximadamente aprox
    euro euros eur eurazo eurazos centimo centimos
    avisame avisadme avisar avisarme avisarnos avises aviseis
    alertame alertadme
    borra borrala borralo borralas borralos borrar borrame
    elimina eliminala eliminalo eliminalas eliminalos eliminar eliminame
    quita quitala quitalo quitalas quitalos quitar quitame quitale
    cambia cambiala cambialo cambialas cambialos cambiar cambiame cambiale
    cambiamele cambiamele cambiamela cambiamelo
    modifica modificame modificar actualiza actualizame actualizar
    ajusta ajustame ajustar corrige corrigeme corregir rectifica rectificar
    pon ponme ponle sube subele subeme subir baja bajale bajame bajar
    cancela cancelame cancelar anula anulame anular olvida olvidalo olvidala
    olvidate descarta descartala descartalo fuera
    deja dejad dejar dejame dejala dejalo dejo
    quiero quieres queria querria gustaria
    lista listame listar dime dame dime muestrame muestra ensename ensenadme
    revisa ver numero numeros num
    precio precios limite limites umbral umbrales maximo maxima minimo minima
    tope presupuesto condicion condiciones restriccion restricciones regla
    reglas horario horarios unidad unidades litro litros kilo kilos kilogramo
    kilogramos cantidad cantidades volumen temperatura temperaturas grado grados
    marca marcas tienda tiendas comercio comercios
    hola buenas buenos gracias favor oye please porfa
    """
)

# The generic words for "a deal": they say that the alert exists, not which
# one it is, so "cualquier aviso" and "cualquier chollo" are the same phrase
# once they are gone.
_GENERIC_ALERT_WORDS = _word_set(
    """
    alerta alertas aviso avisos chollo chollos oferta ofertas oportunidad
    oportunidades deal deals
    """
)

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def content_tokens(text: str) -> frozenset[str]:
    """The words of a sentence that can identify an alert, canonicalised.

    Filler, the management verbs, the currency, bare numbers and the generic
    words for an alert are dropped: what is left is the product, the brand, the
    shop or the qualifier the operator actually wrote.
    """
    tokens = set()
    for word in _WORD_RE.findall(fold(text)):
        if word.isdigit() or word in _FILLER_WORDS or word in _GENERIC_ALERT_WORDS:
            continue
        tokens.add(word)
    return frozenset(tokens)


# The property of an alert a management sentence can change. It is deliberately
# narrow: a word that is not here ("alerta", "zapatillas") names the alert
# instead of a property of it, which is what keeps "quita esa alerta" a
# deletion and "quita el límite" an update.
_PRICE_PROPERTY = r"(?:precio|l[ií]mite|umbral|m[aá]ximo|tope|presupuesto)"
_OTHER_PROPERTY = (
    r"(?:horario|tiendas?|comercios?|unidad|l[ií]tro|kilos?|kilogramos?|"
    r"cantidad|volumen|temperatura|grados?|marca)"
)
_ANY_PROPERTY = rf"(?:{_PRICE_PROPERTY}|{_OTHER_PROPERTY})"
_DETERMINER = r"(?:el|la|los|las|mi|mis|ese|esa|esos|esas|este|esta|estos|estas)"

# The unit a temperature carries. Degrees are a condition of the deals, never
# money ("por menos de 700 €") and never a deletion cue.
_DEGREES = r"(?:°\s*[cf]?|grados?)"
_TEMPERATURE_CUE = re.compile(rf"\d+\s*{_DEGREES}")

# Changing a property of an existing alert.
_CHANGE_VERBS = re.compile(
    r"\b(?:cambia|cambiar|cambiame|cambiale|cambiamela|modifica|modificame|"
    r"modificar|actualiza|actualizame|actualizar|ajusta|ajustame|ajustar|"
    r"corrige|corregir|rectifica|rectificar|sube|subir|baja|bajar)\b"
)
_SET_PROPERTY = re.compile(
    rf"\b(?:pon|ponme|ponle|establece|fija|fijame|deja)\b\s+{_DETERMINER}?\s*"
    rf"{_ANY_PROPERTY}\b"
)
_REMOVE_PROPERTY = re.compile(
    rf"\b(?:quita|quitar|quitame|quitale|borra|borrar|elimina|eliminar|saca)\b"
    rf"\s+{_DETERMINER}?\s*{_ANY_PROPERTY}\b"
)
# Removing a price limit is the change with a price on it: the number in
# "quita el límite de 200 €" is the limit that is being removed, not a new one.
_REMOVE_PRICE_LIMIT = re.compile(
    rf"\b(?:quita|quitar|quitame|quitale|borra|borrar|elimina|eliminar|saca)\b"
    rf"\s+{_DETERMINER}?\s*{_PRICE_PROPERTY}\b"
)

# Getting rid of an existing alert.
_DELETE_CUES = re.compile(
    r"\b(?:borra|borrar|borrame|elimina|eliminar|eliminame|quita|quitar|quitame|"
    r"quitale|cancela|cancelar|cancelame|anula|anular|anulame|olvida|olvidalo|"
    r"olvidala|olvidate|descarta|descartala|descartalo)\b"
)
# "Ya no quiero X" is a removal, but it is also how a temperature condition is
# stated ("no quiero chollos por debajo de 100 grados" = the deal must reach
# 100): the degrees decide which of the two it is.
_SOFT_DELETE_CUES = re.compile(
    r"\bdeja de avisarme\b|\bdejad de avisarme\b|\bdeja de avisarnos\b"
    r"|\bya no quiero\b|\bno quiero mas\b|\bno quiero recibir\b"
    r"|\bno me avises\b|\bno me aviseis\b|\bdejar de recibir\b"
    rf"|\bno quiero\s+(?:mas\s+)?{_DETERMINER}\s+"
    r"(?:alerta|aviso|chollo|oferta|oportunidad|deal)\b"
)

# Asking for the list of stored alerts.
_LIST_CUES = re.compile(
    r"\b(?:que|cuales|cuantas)\s+alertas?\b"
    r"|\blista(?:me|r)?\s+(?:mis\s+|las\s+|todas\s+(?:las\s+)?)?alertas?\b"
    r"|\bmis\s+alertas?\b"
    r"|\b(?:ver|muestrame|muestra|dame|ensename)\s+"
    r"(?:mis\s+|las\s+|todas\s+(?:las\s+)?)?alertas?\b"
    r"|\balertas?\s+(?:activas|configuradas|guardadas|tengo|hay)\b"
)

# Creating a new alert. The management cues are checked first, so the creation
# words a deletion can contain ("deja de avisarme") never win.
_CREATE_CUES = re.compile(
    r"\bavisame\b|\bavisadme\b|\bnotificame\b|\bnotificadme\b"
    r"|\bquiero que me avises\b|\bquiero avisos?\b|\bquiero (?:una |la )?alertas?\b"
    r"|\balertas?\s+de\b|\bcrea(?:me)? (?:una |la )?alerta\b"
    r"|\banade(?:me)? (?:una |la )?alerta\b|\bnueva alerta\b"
    r"|\bvigila(?:me)?\b|\bmonitoriza(?:me)?\b|\bsigueme\b"
)


def classify_alert_operation(text: str) -> AlertOperation:
    """The operation a sentence asks for, decided before any extraction.

    The management cues win over the creation ones, because a deletion can
    contain a creation word ("deja de avisarme de los chollos de menos de
    200 €"). `UNKNOWN` means the sentence said nothing this router recognizes:
    it keeps the original create/interpret path untouched.
    """
    folded = fold(text)
    if not folded:
        return "UNKNOWN"
    if _asks_to_change(folded):
        return "UPDATE_ALERT"
    if _DELETE_CUES.search(folded):
        return "DELETE_ALERT"
    if _SOFT_DELETE_CUES.search(folded) and not _TEMPERATURE_CUE.search(folded):
        return "DELETE_ALERT"
    if _LIST_CUES.search(folded):
        return "LIST_ALERTS"
    if _CREATE_CUES.search(folded):
        return "CREATE_ALERT"
    return "UNKNOWN"


def _asks_to_change(folded: str) -> bool:
    """True when the sentence changes a property of an existing alert."""
    return bool(
        _CHANGE_VERBS.search(folded)
        or _SET_PROPERTY.search(folded)
        or _REMOVE_PROPERTY.search(folded)
    )


# --- What alert a sentence refers to ---------------------------------------- #

# A number may carry a thousands separator and decimals ("1.200,50"): the
# amount is only read whole, never as the part of a longer number.
_NUMBER = r"\d+(?:[.,]\d+)*"
_CURRENCY = r"(?:€|euros?|eur)"
# A number is money when the sentence says so: it carries a currency, or a
# comparator introduces it. A number with a temperature unit is never money.
# The `(?![\d.,])` is what keeps the patterns from matching the first digits of
# a longer number ("10" out of "100 grados").
_NOT_DEGREES = rf"(?![\d.,])(?!\s*{_DEGREES})"
_MONEY_VALUE = re.compile(rf"({_NUMBER}){_NOT_DEGREES}\s*{_CURRENCY}")
_COMPARED_VALUE = re.compile(
    rf"(?:menos de|por menos de|por debajo de|inferior(?:es)? a|hasta|"
    rf"como maximo|maximo de|maximo|no mas de|<)\s*({_NUMBER}){_NOT_DEGREES}"
)
# "borra la de 200": the amount of an alert referred to by its condition. Two
# digits at least, so "la alerta 2" is a number and not a price.
_BARE_VALUE = re.compile(rf"\bde\s+(\d[\d.,]*\d){_NOT_DEGREES}")
# "de 200 a 150", "cambia 200 por 150": the first number is the old value (the
# one that identifies the alert) and the second the one being asked for.
_CHANGE_PAIR = re.compile(
    rf"(?:de\s+)?({_NUMBER}){_NOT_DEGREES}\s*(?:{_CURRENCY})?\s*(?:a|por)\s*"
    rf"({_NUMBER}){_NOT_DEGREES}\s*(?:{_CURRENCY})?"
)
# "de 8:00 a 23:00" is a schedule, not a price: the times are removed before
# any number is read as money.
_TIME_OF_DAY = re.compile(r"\b\d{1,2}:\d{2}\b")

_RULE_NUMBER = re.compile(r"#\s*(\d+)|\b(?:alerta|aviso|numero|n)\s+(\d+)\b")
_DEICTIC = re.compile(
    r"\b(?:esa|ese|eso|esas|esos|esta|este|esto|estas|estos|aquella|aquel)\b"
    r"|\bla ultima\b|\blo ultimo\b|\bla de antes\b|\bel de antes\b|\blos de antes\b"
)
_UNIT_CUES = (
    (re.compile(r"\bpor unidad\b|\bpor ud\b|/\s*ud\b"), "unit"),
    (re.compile(r"\bpor litro\b|/\s*l\b"), "liter"),
    (re.compile(r"\bpor kilo\b|\bpor kilogramo\b|/\s*kg\b"), "kilogram"),
)


@dataclass(frozen=True)
class AlertReference:
    """What a management sentence says about the alert it refers to."""

    text: str = ""
    rule_id: int | None = None
    deictic: bool = False
    # The price the sentence quotes as the alert's own condition, and the price
    # the update asks for. "cambia 200 por 150" identifies by 200 and changes
    # to 150; "quita el límite de 200" identifies by 200 and removes the limit.
    identity_price: Decimal | None = None
    new_price: Decimal | None = None
    price_unit: str | None = None
    remove_limit: bool = False
    include_merchants: tuple[str, ...] = ()
    exclude_merchants: tuple[str, ...] = ()
    notification_window: NotificationWindow | None = None
    tokens: frozenset[str] = frozenset()

    @property
    def has_content(self) -> bool:
        """True when the sentence says something about *which* alert it means."""
        return bool(
            self.rule_id is not None
            or self.identity_price is not None
            or self.include_merchants
            or self.exclude_merchants
            or self.notification_window is not None
            or self.tokens
        )

    @property
    def has_change(self) -> bool:
        """True when the sentence says *what* to change, not only which alert."""
        return bool(
            self.remove_limit
            or self.new_price is not None
            or self.include_merchants
            or self.exclude_merchants
            or self.notification_window is not None
        )


def read_alert_reference(
    text: str, operation: AlertOperation | None = None
) -> AlertReference:
    """Read the alert a sentence refers to, and the change it asks for."""
    folded = fold(text)
    operation = operation or classify_alert_operation(text)
    # Times are a schedule, never a price: they are masked before any number is
    # read, so "cambia el horario de 8:00 a 23:00" cannot change a price.
    masked = _TIME_OF_DAY.sub(" ", folded)
    remove_limit = bool(_REMOVE_PRICE_LIMIT.search(masked))
    pair = _CHANGE_PAIR.search(masked)
    stated = _stated_price(masked)
    identity_price = new_price = None
    if pair is not None:
        identity_price, new_price = _money(pair.group(1)), _money(pair.group(2))
    elif remove_limit:
        # The number of "quita el límite de 200 €" is the limit being removed.
        identity_price = stated
    elif stated is not None and operation == "UPDATE_ALERT":
        new_price = stated
    else:
        identity_price = stated
    mentions = extract_merchant_mentions(text)
    window = extract_notification_window(text)
    return AlertReference(
        text=text,
        rule_id=_rule_number(masked),
        deictic=bool(_DEICTIC.search(masked)),
        identity_price=identity_price,
        new_price=new_price,
        price_unit=_unit_cue(masked),
        remove_limit=remove_limit,
        include_merchants=mentions.allowed,
        exclude_merchants=mentions.excluded,
        notification_window=_rule_window(window),
        tokens=content_tokens(folded),
    )


def _stated_price(folded: str) -> Decimal | None:
    """The last price the sentence states as a condition, if it states one."""
    values = [
        (match.start(), match.group(1)) for match in _COMPARED_VALUE.finditer(folded)
    ]
    values += [
        (match.start(), match.group(1)) for match in _MONEY_VALUE.finditer(folded)
    ]
    values += [
        (match.start(), match.group(1)) for match in _BARE_VALUE.finditer(folded)
    ]
    if not values:
        return None
    values.sort()
    return _money(values[-1][1])


def _money(raw: str) -> Decimal:
    """`200`, `200.50` and `1.200,50` all become the decimal they mean."""
    text = raw.strip()
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", text):
        # A dot every three digits is a thousands separator, not a decimal one.
        text = text.replace(".", "")
    try:
        return Decimal(text)
    except InvalidOperation:  # pragma: no cover - the patterns only match digits
        raise ValueError(f"«{raw}» no es un precio") from None


def _rule_number(folded: str) -> int | None:
    match = _RULE_NUMBER.search(folded)
    if match is None:
        return None
    return int(match.group(1) or match.group(2))


def _unit_cue(folded: str) -> str | None:
    for pattern, unit in _UNIT_CUES:
        if pattern.search(folded):
            return unit
    return None


def _rule_window(window) -> NotificationWindow | None:
    """The rule-shaped window of the sentence, or None when it names none."""
    if window is None:
        return None
    return NotificationWindow(
        start=window.start, end=window.end, timezone=window.timezone
    )


# --- Which stored alert matches --------------------------------------------- #

# A candidate is plausible when it reaches this score, and clearly compatible
# when it is the only one that does and no other one comes close.
PLAUSIBLE = 0.5
CLEAR = 0.75
MARGIN = 0.25


@dataclass(frozen=True)
class AlertCandidate:
    """One stored alert, as the resolution compares it."""

    rule_id: int
    rule: AlertRule
    original_text: str | None = None
    enabled: bool = True
    # A rule persisted before the structured rules existed carries a
    # `price_unit` column that is not a real dimension of the alert.
    legacy: bool = False

    @property
    def tokens(self) -> frozenset[str]:
        """The words that identify this alert: query, product, brand, shop…"""
        parts = [
            self.rule.query,
            self.rule.product or "",
            self.rule.brand or "",
            self.rule.category or "",
            self.original_text or "",
            *self.rule.include_merchants,
            *self.rule.exclude_merchants,
        ]
        return content_tokens(" ".join(part for part in parts if part))


def alert_candidates(repository, user_id=None) -> tuple[AlertCandidate, ...]:
    """Every stored alert, resolved through the canonical rule boundary."""
    candidates = []
    for row in repository.list_alert_rules(user_id=user_id):
        rule_id = row[0]
        candidates.append(
            AlertCandidate(
                rule_id=rule_id,
                rule=repository.rule_from_listing(row),
                original_text=repository.alert_rule_original_text(rule_id),
                enabled=bool(row[6]),
                legacy=repository.load_alert_rule(rule_id) is None,
            )
        )
    return tuple(candidates)


@dataclass(frozen=True)
class AlertResolution:
    """The outcome of looking one reference up among the stored alerts."""

    status: Literal["unique", "ambiguous", "none", "context_required"]
    matches: tuple[AlertCandidate, ...] = ()

    @property
    def match(self) -> AlertCandidate | None:
        return self.matches[0] if len(self.matches) == 1 else None


def resolve_alert_reference(
    reference: AlertReference,
    candidates,
    context_ids=(),
) -> AlertResolution:
    """Find the stored alert a management sentence refers to.

    An explicit number wins; a sentence that only points ("esa alerta") is
    resolved with the conversational context; anything else is compared with
    the stored alerts, structured facts first (price, shop) and the normalized
    text of the sentence last. One clearly compatible candidate is a match, two
    plausible ones are an ambiguity the operator has to settle.
    """
    by_id = {candidate.rule_id: candidate for candidate in candidates}
    if reference.rule_id is not None:
        candidate = by_id.get(reference.rule_id)
        if candidate is not None:
            return _single(candidate)
        # The number of "la alerta 2" is the operator's reference, and an
        # operator may name one that no longer exists: the sentence is then
        # resolved by what it says, exactly as if the number were not there.
    if not reference.has_content:
        # "Quita esa alerta": the sentence points instead of naming, so only
        # the last alert(s) the bot showed can settle it.
        return _from_context(by_id, context_ids)
    scored = []
    for candidate in candidates:
        score = _score(reference, candidate)
        if score is not None and score >= PLAUSIBLE:
            scored.append((score, candidate))
    if not scored:
        if (
            reference.identity_price is None
            and not reference.tokens
            and not reference.include_merchants
            and reference.notification_window is None
        ):
            # The sentence only said *what* to change ("pon el precio a 150 €"):
            # with nothing to tell the alerts apart, it means the one the bot
            # showed last.
            return _from_context(by_id, context_ids)
        return AlertResolution("none")
    scored.sort(key=lambda item: (-item[0], item[1].rule_id))
    best = scored[0][0]
    top = [_candidate for score, _candidate in scored if score >= best - 1e-9]
    if len(top) == 1 and (
        len(scored) == 1 or best >= CLEAR or best - scored[1][0] >= MARGIN
    ):
        return _single(top[0])
    if reference.deictic:
        # "esa alerta" among several equally good matches: the last alert the
        # bot showed settles the tie when it points at exactly one of them.
        pointed = [
            candidate for candidate in top if candidate.rule_id in tuple(context_ids)
        ]
        if len(pointed) == 1:
            return _single(pointed[0])
    return AlertResolution("ambiguous", tuple(top))


def _single(candidate) -> AlertResolution:
    return AlertResolution("unique", (candidate,))


def _from_context(by_id, context_ids) -> AlertResolution:
    """Resolve a bare "esa alerta" with the last alert(s) the bot showed."""
    pointed = tuple(by_id[rule_id] for rule_id in context_ids if rule_id in by_id)
    if len(pointed) == 1:
        return _single(pointed[0])
    if pointed:
        return AlertResolution("ambiguous", pointed)
    return AlertResolution("context_required")


def _score(reference: AlertReference, candidate: AlertCandidate) -> float | None:
    """How well one stored alert matches the reference, or None when it cannot.

    The structured facts are what identify an alert: a price the sentence
    quotes has to be the price of the alert, and a shop it names has to be a
    shop of the alert. The normalized text of the sentence only refines a
    match the structured facts already support, or stands on its own when the
    sentence gives no structured fact at all.
    """
    rule = candidate.rule
    structured = False
    if reference.identity_price is not None:
        if _rule_price(rule) != reference.identity_price:
            return None
        structured = True
    if reference.include_merchants and _shares_a_shop(reference, rule):
        structured = True
    if not reference.tokens:
        return 1.0 if structured else None
    overlap = len(reference.tokens & candidate.tokens) / len(reference.tokens)
    if structured:
        return 0.5 + 0.5 * overlap
    return overlap


def _rule_price(rule: AlertRule) -> Decimal | None:
    """The single price dimension the alert really carries, if any."""
    constraints = rule.constraints
    for value in (
        constraints.max_price,
        constraints.max_price_per_unit,
        constraints.max_price_per_liter,
    ):
        if value is not None:
            return Decimal(value)
    return None


def _shares_a_shop(reference: AlertReference, rule: AlertRule) -> bool:
    """True when a shop the sentence names is a shop of the alert."""
    from .merchants import normalize_merchant

    mentioned = {normalize_merchant(name) for name in reference.include_merchants}
    shops = {
        normalize_merchant(name)
        for name in (*rule.include_merchants, *rule.exclude_merchants)
    }
    return bool(mentioned & shops)


# --- Changing an existing alert --------------------------------------------- #


def updated_rule(
    rule: AlertRule, reference: AlertReference, *, legacy: bool = False
) -> AlertRule:
    """The same alert with only the properties the sentence asked to change.

    The product, the brand and every property the sentence does not mention
    stay exactly as they were: an update rewrites the stored alert, it never
    rebuilds it from scratch and never creates a second one.
    """
    data = rule.model_dump()
    constraints = rule.constraints.model_dump()
    if reference.remove_limit:
        for name in ("max_price", "max_price_per_unit", "max_price_per_liter"):
            constraints[name] = None
    elif reference.new_price is not None:
        dimension = _price_dimension(rule, reference.price_unit, legacy=legacy)
        for name in ("max_price", "max_price_per_unit", "max_price_per_liter"):
            constraints[name] = None
        constraints[dimension] = str(reference.new_price)
    data["constraints"] = constraints
    if reference.include_merchants:
        data["include_merchants"] = list(reference.include_merchants)
    if reference.exclude_merchants:
        data["exclude_merchants"] = list(reference.exclude_merchants)
    if reference.notification_window is not None:
        data["notification_window"] = reference.notification_window.model_dump()
    return AlertRule.model_validate(data)


def _price_dimension(rule: AlertRule, unit: str | None, *, legacy: bool) -> str:
    """Which price dimension a new price belongs to.

    A price written as money with no unit is the total price of the deal,
    exactly as in the creation path ("por menos de 200 €" is an absolute
    price). Only when the alert already uses a per-unit dimension of its own —
    and it is a real dimension, not the legacy column of a rule persisted
    before the structured rules existed — the new price stays in it.
    """
    if unit == "unit":
        return "max_price_per_unit"
    if unit == "liter":
        return "max_price_per_liter"
    if unit == "kilogram":
        return "max_price"
    constraints = rule.constraints
    if not legacy and constraints.max_price is None:
        if constraints.max_price_per_unit is not None:
            return "max_price_per_unit"
        if constraints.max_price_per_liter is not None:
            return "max_price_per_liter"
    return "max_price"
