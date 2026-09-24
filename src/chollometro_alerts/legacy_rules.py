"""Explicit, conservative repair of legacy alert identities.

Legacy rows predate structured product/brand persistence.  Their ``query`` is
still a discovery term, so it is never promoted to a matcher condition unless
this module can prove that a single proper-name token is the subject and the
remaining suffix is only the old price-search fragment.
"""

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from .alert_rule import AlertConstraints, AlertRule

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_SEARCH_SUFFIXES = {"p", "pr", "por", "precio", "menos", "debajo", "de"}
_GENERIC_TERMS = {
    "barato",
    "baratos",
    "chollo",
    "chollos",
    "deal",
    "oferta",
    "ofertas",
    "producto",
    "productos",
    "supermercado",
    "zapatilla",
    "zapatillas",
}


@dataclass(frozen=True)
class LegacyRuleProposal:
    rule_id: int
    query: str
    classification: str
    brand: str | None = None
    reason: str = ""

    @property
    def actionable(self) -> bool:
        return self.classification == "LEGACY_SAFE_TO_ENRICH" and self.brand is not None


def _fold(value: str) -> str:
    return (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
        .casefold()
    )


def infer_legacy_brand(query: str) -> tuple[str | None, str]:
    """Infer only a single explicit proper-name brand from a legacy query."""
    raw_tokens = _TOKEN_RE.findall(query)
    tokens = list(raw_tokens)
    while len(tokens) > 1 and _fold(tokens[-1]) in _SEARCH_SUFFIXES:
        tokens.pop()
    if len(tokens) != 1:
        return None, "query has no single unambiguous subject token"
    candidate = tokens[0]
    if _fold(candidate) in _GENERIC_TERMS:
        return None, "query is a generic category or shopping term"
    if not (candidate[:1].isupper() or candidate.isupper()):
        return None, "query token is not an explicit proper name"
    return candidate, "single explicit proper-name token after search suffix"


def classify_legacy_rule(row) -> LegacyRuleProposal:
    """Classify one alert_rules row without changing it."""
    rule_id, query, product, brand, structured_rule = row
    if structured_rule or product or brand:
        return LegacyRuleProposal(
            rule_id, query, "STRUCTURED", brand, "already structured"
        )
    if _fold(query).strip() in _GENERIC_TERMS:
        return LegacyRuleProposal(rule_id, query, "GENERIC", reason="generic query")
    inferred, reason = infer_legacy_brand(query)
    if inferred is not None:
        return LegacyRuleProposal(
            rule_id, query, "LEGACY_SAFE_TO_ENRICH", inferred, reason
        )
    return LegacyRuleProposal(rule_id, query, "LEGACY_AMBIGUOUS", reason=reason)


def audit_legacy_rules(repository) -> list[LegacyRuleProposal]:
    rows = repository.db.execute(
        """SELECT id, query, product_type, brand, structured_rule
        FROM alert_rules ORDER BY id"""
    ).fetchall()
    return [classify_legacy_rule(row) for row in rows]


def _legacy_rule_from_row(row, brand: str) -> AlertRule:
    _, query, product, _, maximum, unit, _, _, _, _, _, _, _ = row
    kwargs = {}
    if unit == "liter":
        kwargs["max_price_per_liter"] = maximum
    elif unit == "unit":
        kwargs["max_price_per_unit"] = maximum
    else:
        kwargs["max_price"] = maximum
    return AlertRule(
        query=query,
        product=product,
        brand=brand,
        constraints=AlertConstraints(**kwargs),
    )


def repair_legacy_rules(
    repository, *, dry_run: bool = True
) -> list[LegacyRuleProposal]:
    """Audit and optionally repair only high-confidence legacy rows.

    The update is in-place and transactional: rule ids, enabled/state,
    creation time, observations and matches remain untouched.  A unique audit
    row makes a second application a no-op.
    """
    proposals = [
        proposal for proposal in audit_legacy_rules(repository) if proposal.actionable
    ]
    if dry_run or not proposals:
        return proposals
    db = repository.db
    db.execute(
        """CREATE TABLE IF NOT EXISTS legacy_rule_repairs (
            rule_id INTEGER PRIMARY KEY,
            old_relevance TEXT NOT NULL,
            new_relevance TEXT NOT NULL,
            reason TEXT NOT NULL,
            repaired_at TEXT NOT NULL
        )"""
    )
    now = datetime.now(UTC).isoformat()
    for proposal in proposals:
        row = db.execute(
            "SELECT * FROM alert_rules WHERE id=?", (proposal.rule_id,)
        ).fetchone()
        if row is None:
            continue
        if db.execute(
            "SELECT 1 FROM legacy_rule_repairs WHERE rule_id=?", (proposal.rule_id,)
        ).fetchone():
            continue
        assert proposal.brand is not None
        rule = _legacy_rule_from_row(row, proposal.brand)
        db.execute(
            """UPDATE alert_rules
            SET brand=?, structured_rule=?, schema_version=?
            WHERE id=? AND structured_rule IS NULL AND brand IS NULL
              AND product_type IS NULL""",
            (
                proposal.brand,
                rule.model_dump_json(),
                rule.schema_version,
                proposal.rule_id,
            ),
        )
        db.execute(
            """INSERT OR IGNORE INTO legacy_rule_repairs
            (rule_id, old_relevance, new_relevance, reason, repaired_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                proposal.rule_id,
                json.dumps({"product": None, "brand": None}, ensure_ascii=False),
                json.dumps(
                    {"product": rule.product, "brand": rule.brand}, ensure_ascii=False
                ),
                proposal.reason,
                now,
            ),
        )
    db.commit()
    return proposals
