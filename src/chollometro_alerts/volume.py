import re
from decimal import Decimal

VOLUME_UNIT = r"l|litro?s?|ml|mililitros?|cl|centilitros?"
VOLUME_VALUE = rf"(?P<volume>\d+(?:[,.]\d+)?)\s*(?P<unit>{VOLUME_UNIT})\b"
VOLUME_PATTERNS = (
    re.compile(
        rf"(?:\b(?:pack|caja)\s+(?:de\s+)?)?"
        rf"(?P<units>\d+)\s*(?:x|×)\s*{VOLUME_VALUE}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?P<units>\d+)\s+(?:latas?|botellas?|briks?|bricks?)"
        rf"(?:\s+[\w]+)*?\s+(?:de\s+)?{VOLUME_VALUE}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:pack|caja)\s+(?P<units>\d+)\s+"
        rf"(?:latas?|botellas?|briks?|bricks?)?"
        rf"(?:\s+[\w]+)*?\s+(?:de\s+)?{VOLUME_VALUE}",
        re.IGNORECASE,
    ),
)
SINGLE_VOLUME_RE = re.compile(
    rf"(?P<volume>\d+(?:[,.]\d+)?)\s*(?P<unit>{VOLUME_UNIT})\b",
    re.IGNORECASE,
)


def extract_volume(text: str):
    match = next(
        (
            found
            for pattern in VOLUME_PATTERNS
            if (found := pattern.search(text or "")) is not None
        ),
        None,
    )
    if not match:
        # "caja 6 litros" expresses six one-litre units in retail copy.
        total = re.search(
            r"\b(?:caja|pack)\s+(?:de\s+)?(?P<units>\d+)\s+(?P<unit>litros?|l)\b",
            text or "",
            re.IGNORECASE,
        )
        if total:
            units = int(total.group("units"))
            liters = Decimal(1)
            return units, liters, Decimal(units)
        # A product name followed by a volume is one individual unit. This is
        # deliberately conservative: without a product/volume expression there
        # is no inferred quantity.
        single = SINGLE_VOLUME_RE.search(text or "")
        if not single:
            return None
        units = 1
        value = Decimal(single.group("volume").replace(",", "."))
        unit = single.group("unit").casefold()
        if unit.startswith(("ml", "mil")):
            liters = value / Decimal(1000)
        elif unit.startswith(("cl", "cent")):
            liters = value / Decimal(100)
        else:
            liters = value
        return units, liters, liters
    units = int(match.group("units"))
    value = Decimal(match.group("volume").replace(",", "."))
    unit = match.group("unit").casefold()
    if unit.startswith(("ml", "mil")):
        liters = value / Decimal(1000)
    elif unit.startswith(("cl", "cent")):
        liters = value / Decimal(100)
    else:
        liters = value
    return units, liters, units * liters
