"""Demographic normalisation — FR-2.5 (optional, cheap, built anyway).

The supplied samples redact `age`, `gender`, `patient_name`, `doctor_name`,
`uhid`, and `claim_no` as `[X REDACTED]` literals, so this module cannot be
demonstrated on real sample output — it is proven by unit tests against
synthetic values instead. See ASSUMPTIONS.md D-11.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from dateutil import parser as dateutil_parser

from src.config_loader import get_document_type_mappings

_AGE_PATTERN = re.compile(r"^\s*(?:(\d+)\s*Y)?\s*(?:(\d+)\s*M)?\s*(?:(\d+)\s*D)?\s*$", re.IGNORECASE)
_PLAIN_NUMBER = re.compile(r"^\s*\d+(\.\d+)?\s*$")

_GENDER_MAP = {
    "m": "MALE", "male": "MALE", "1": "MALE",
    "f": "FEMALE", "female": "FEMALE", "2": "FEMALE",
    "o": "OTHER", "other": "OTHER",
}

_PLACEHOLDER_DATES = {"dd/mm/yyyy", "yyyy-mm-dd", "mm/dd/yyyy", ""}


@dataclass
class DemographicField:
    value: str | None
    redacted: bool = False
    flags: list[str] = field(default_factory=list)


def _redaction_pattern() -> str:
    return get_document_type_mappings()["pii"]["redaction_pattern"]


def is_redacted(raw: str | None) -> bool:
    if raw is None:
        return False
    return bool(re.match(_redaction_pattern(), raw.strip()))


def normalise_redactable_field(raw: str | None) -> DemographicField:
    """Detects the `[X REDACTED]` literal pattern and stores null + a flag
    rather than the literal string. Applies to any freeform demographic or
    identifying field."""
    if raw is None or raw.strip() == "":
        return DemographicField(None)
    if is_redacted(raw):
        return DemographicField(None, redacted=True, flags=["redacted"])
    return DemographicField(raw.strip())


def normalise_gender(raw: str | None) -> DemographicField:
    field_ = normalise_redactable_field(raw)
    if field_.value is None:
        return field_
    canonical = _GENDER_MAP.get(field_.value.strip().lower())
    if canonical is None:
        return DemographicField(None, flags=["gender_unrecognised"])
    return DemographicField(canonical)


@dataclass
class AgeResult:
    age_years: float | None
    age_text: str | None
    redacted: bool = False
    flags: list[str] = field(default_factory=list)


def normalise_age(raw: str | None) -> AgeResult:
    if raw is None or raw.strip() == "":
        return AgeResult(None, raw)
    if is_redacted(raw):
        return AgeResult(None, raw, redacted=True, flags=["redacted"])

    text = raw.strip()

    if _PLAIN_NUMBER.match(text):
        return AgeResult(round(float(text), 2), raw)

    m = _AGE_PATTERN.match(text)
    if m and any(m.groups()):
        years, months, days = (int(g) if g else 0 for g in m.groups())
        age_years = years + months / 12.0 + days / 365.25
        return AgeResult(round(age_years, 2), raw)

    return AgeResult(None, raw, flags=["age_unparseable"])


@dataclass
class DateResult:
    iso_date: str | None
    original: str | None
    flags: list[str] = field(default_factory=list)


def normalise_date(raw: str | None) -> DateResult:
    if raw is None or raw.strip() == "":
        return DateResult(None, raw)

    text = raw.strip()
    if text.lower() in _PLACEHOLDER_DATES:
        return DateResult(None, raw, flags=["placeholder_date"])

    try:
        parsed = dateutil_parser.parse(text, dayfirst=True, fuzzy=False)
        return DateResult(parsed.date().isoformat(), raw)
    except (ValueError, OverflowError):
        # e.g. "LAB10945" — clearly not a date, not a parsing edge case.
        return DateResult(None, raw, flags=["date_unparseable"])


def tokenise_identifier(raw: str | None, method: str = "sha256_truncated_16") -> str | None:
    """Any identifier arriving UNREDACTED is hashed rather than stored raw.
    Production path: Cloud DLP. See ASSUMPTIONS.md S-4."""
    if raw is None or raw.strip() == "" or is_redacted(raw):
        return None
    digest = hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()
    if method == "sha256_truncated_16":
        return digest[:16]
    return digest
