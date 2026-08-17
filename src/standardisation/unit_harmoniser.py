"""Unit harmonisation — FR-2.4.

Two layers, per `config/unit_conversions.yaml`:
  1. Aliases  — spelling/casing/OCR variants of the SAME unit (no value change).
  2. Factors  — genuinely different units for one analyte (value is scaled).

Also implements the deliberate restraint documented in ASSUMPTIONS.md D-6:
rows where the unit and range plainly belong to a different analyte
(`aemoglobin` with `unit: cell/cu.mm`, `range: 4000-10000` — WBC data on a
haemoglobin row) are FLAGGED, never auto-corrected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.config_loader import get_reference_ranges, get_test_dictionary, get_unit_conversions


@dataclass
class HarmoniseResult:
    unit_canonical: str | None
    result_value: float | None
    flags: list[str] = field(default_factory=list)


class UnitHarmoniser:
    def __init__(self, unit_conversions: dict | None = None, test_dictionary: dict | None = None,
                 reference_ranges: dict | None = None):
        self.cfg = unit_conversions or get_unit_conversions()
        self.test_dictionary = test_dictionary or get_test_dictionary()
        self.reference_ranges = (reference_ranges or get_reference_ranges())["ranges"]
        self._build_indices()

    def _build_indices(self) -> None:
        # unit variant (lower) -> canonical family label, e.g. "gm/di" -> "g/dL"
        self.alias_family: dict[str, str] = {}
        for family, variants in self.cfg["aliases"].items():
            for v in variants:
                if v:  # skip the deliberate "" entry under "ratio"
                    self.alias_family[v.lower()] = family

        # canonical family -> set of canonical test names whose canonical_unit is that family
        self.family_owners: dict[str, set[str]] = {}
        for canonical, spec in self.test_dictionary["tests"].items():
            fam = spec.get("canonical_unit")
            if fam:
                self.family_owners.setdefault(fam, set()).add(canonical)

        self.invalid_cfg = self.cfg["invalid_units"]
        self.known_junk_lower = {j.lower() for j in self.invalid_cfg.get("known_junk", [])}
        self.mismatch_cfg = self.cfg["mismatch_detection"]

    def _resolve_family(self, raw_unit: str) -> str:
        return self.alias_family.get(raw_unit.lower(), raw_unit)

    def _is_invalid_unit(self, raw_unit: str) -> bool:
        if raw_unit.lower() in self.known_junk_lower:
            return True
        if re.match(self.invalid_cfg["range_like_pattern"], raw_unit):
            return True
        if re.match(self.invalid_cfg["multi_unit_pattern"], raw_unit):
            return True
        return False

    def _lookup_factor(self, test_canonical: str, raw_unit: str, family: str) -> tuple[float | None, str | None]:
        conv = self.cfg["conversions"].get(test_canonical)
        if not conv:
            return None, None
        from_map = conv["from"]
        for candidate in (raw_unit, family):
            for key, spec in from_map.items():
                if key.lower() == candidate.lower():
                    return spec["factor"], conv["canonical_unit"]
        return None, conv["canonical_unit"]

    def harmonise(self, unit_original: str | None, test_canonical: str | None,
                  result_value: float | None, range_low: float | None = None,
                  range_high: float | None = None) -> HarmoniseResult:
        flags: list[str] = []

        if test_canonical is None or test_canonical not in self.test_dictionary["tests"]:
            # Unresolved / qualitative / narrative tests carry no unit contract.
            return HarmoniseResult(unit_canonical=unit_original, result_value=result_value, flags=flags)

        expected_family = self.test_dictionary["tests"][test_canonical]["canonical_unit"]
        raw_unit = (unit_original or "").strip()

        if raw_unit == "":
            flags.append("unit_missing_assumed_canonical")
            return HarmoniseResult(unit_canonical=expected_family, result_value=result_value, flags=flags)

        if self._is_invalid_unit(raw_unit):
            flags.append(self.invalid_cfg["flag"])
            if self.invalid_cfg.get("fallback_to_canonical"):
                flags.append(self.invalid_cfg["fallback_flag"])
            return HarmoniseResult(unit_canonical=expected_family, result_value=result_value, flags=flags)

        family = self._resolve_family(raw_unit)

        factor, conv_canonical_unit = self._lookup_factor(test_canonical, raw_unit, family)
        if factor is not None:
            converted = result_value * factor if result_value is not None else None
            if factor != 1:
                flags.append("unit_converted")
            return HarmoniseResult(unit_canonical=conv_canonical_unit, result_value=converted, flags=flags)

        # No factor found. Is this simply the same family already (fine), or
        # does it belong to a DIFFERENT analyte's canonical unit (mismatch)?
        if family.lower() == expected_family.lower():
            return HarmoniseResult(unit_canonical=expected_family, result_value=result_value, flags=flags)

        owners = self.family_owners.get(family, set())
        if self.mismatch_cfg.get("enabled") and owners and test_canonical not in owners:
            flags.append(self.mismatch_cfg["on_mismatch"]["flag"])  # unit_test_mismatch
            if self._range_implausible(test_canonical, range_low, range_high):
                flags.append(self.mismatch_cfg["range_plausibility_check"]["flag"])
            # Deliberate: do not auto-correct. Retain the original unit/value.
            return HarmoniseResult(unit_canonical=raw_unit, result_value=result_value, flags=flags)

        flags.append("unit_conversion_unavailable")
        return HarmoniseResult(unit_canonical=expected_family, result_value=result_value, flags=flags)

    def _range_implausible(self, test_canonical: str, range_low: float | None, range_high: float | None) -> bool:
        if range_low is None and range_high is None:
            return False
        ref = self.reference_ranges.get(test_canonical)
        if not ref:
            return False
        tolerance = self.mismatch_cfg["range_plausibility_check"]["tolerance_multiple"]
        lo, hi = ref["low"], ref["high"]
        span = max(hi - lo, 1e-9)
        for v in (range_low, range_high):
            if v is None:
                continue
            if v < lo - tolerance * span or v > hi + tolerance * span:
                return True
        return False
