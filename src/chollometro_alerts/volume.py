import re
from decimal import Decimal

VOLUME_RE = re.compile(
    r"(?:\b(?:pack|caja)\s+)?(?P<units>\d+)\s*(?:x|×|"
    r"(?:botellas?|briks?|latas?)\s+de)\s*"
    r"(?P<volume>\d+(?:[,.]\d+)?)\s*(?P<unit>l|litro?s?|ml|mililitros?|cl|centilitros?)\b",
    re.IGNORECASE,
)


def extract_volume(text: str):
    match = VOLUME_RE.search(text or "")
    if not match:
        # "caja 6 litros" expresses six one-litre units in retail copy.
        total = re.search(
            r"\b(?:caja|pack)\s+(?:de\s+)?(?P<units>\d+)\s+(?P<unit>litros?|l)\b",
            text or "",
            re.IGNORECASE,
        )
        if not total:
            return None
        units = int(total.group("units"))
        liters = Decimal(1)
        return units, liters, Decimal(units)
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
