"""Test name normalisation — FR-2.1, the centrepiece of the pipeline.

Three-tier resolution against `config/test_dictionary.yaml`, and the tier
that fired is recorded on every row:

  1. exact      -> variant matched verbatim after preprocessing   conf 1.00
  2. fuzzy      -> rapidfuzz token_set_ratio >= threshold          conf score/100
  3. unresolved -> below threshold; canonical null, Invalid downstream

Pure function of (raw_name, raw_result, config) -> NormalisationResult, so
this module is trivially unit-testable and would lift into a Beam DoFn
without redesign (see architecture_narrative.md).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from src.config_loader import get_test_dictionary


@dataclass
class NormalisationResult:
    test_name_original: str
    test_name_canonical: str | None
    normalization_method: str          # exact | fuzzy | extracted | unresolved
    normalization_confidence: float
    extracted_value: float | None = None
    is_qualitative: bool = False
    is_narrative: bool = False
    is_panel_header: bool = False
    is_composite_row: bool = False
    best_candidate: str | None = None  # for the unresolved review queue
    flags: list[str] = field(default_factory=list)


class TestNameNormaliser:
    def __init__(self, config: dict | None = None):
        self.config = config or get_test_dictionary()
        self.matching = self.config["matching"]
        self._build_indices()

    # ------------------------------------------------------------------ setup
    def _build_indices(self) -> None:
        self.clinical_lookup: dict[str, str] = {}
        self.clinical_units: dict[str, str] = {}
        for canonical, spec in self.config["tests"].items():
            for variant in spec["variants"]:
                self.clinical_lookup[variant.lower()] = canonical
            self.clinical_units[canonical] = spec.get("canonical_unit")

        self.qualitative_lookup: dict[str, str] = {}
        for canonical, spec in self.config.get("qualitative_tests", {}).get("tests", {}).items():
            for variant in spec["variants"]:
                self.qualitative_lookup[variant.lower()] = canonical

        self.narrative_set: set[str] = set()
        narrative_cfg = self.config.get("narrative_tests", {})
        for key in ("variants", "symptoms", "nursing_chart"):
            for v in narrative_cfg.get(key, []):
                self.narrative_set.add(v.lower())

        # combined pool for fuzzy matching: variant_lower -> (canonical, catalog)
        self.fuzzy_pool: dict[str, tuple[str, str]] = {}
        for v, c in self.clinical_lookup.items():
            self.fuzzy_pool[v] = (c, "clinical")
        for v, c in self.qualitative_lookup.items():
            self.fuzzy_pool[v] = (c, "qualitative")
        for v in self.narrative_set:
            self.fuzzy_pool[v] = (v, "narrative")

        panel_cfg = self._panel_headers_config()
        self.known_panels = {p.lower() for p in panel_cfg.get("known_panels", [])}
        self.panel_suppress_empty = panel_cfg.get("suppress_when_result_empty", True)

    def _panel_headers_config(self) -> dict:
        from src.config_loader import get_document_type_mappings
        return get_document_type_mappings().get("panel_headers", {})

    # -------------------------------------------------------------- pipeline
    def preprocess(self, name: str) -> str:
        cfg = self.matching["preprocessing"]
        s = name
        if cfg.get("strip_whitespace"):
            s = s.strip()
        if cfg.get("collapse_internal_spaces"):
            s = re.sub(r"\s+", " ", s)
        for suf in cfg.get("strip_specimen_suffixes", []):
            if s.upper().endswith(suf.upper()):
                s = s[: -len(suf)]
                s = s.strip()
        if cfg.get("strip_method_annotations"):
            pattern = cfg.get("method_annotation_pattern")
            if pattern:
                s = re.sub(pattern, "", s, flags=re.IGNORECASE).strip()
        if cfg.get("strip_trailing_punctuation"):
            s = re.sub(r"[.,;:]+$", "", s).strip()
        return s

    def _detect_composite(self, name: str) -> bool:
        cfg = self.matching["composite_row_detection"]
        if not cfg.get("enabled"):
            return False
        matches = re.findall(r"-\s*[\d.]+", name)
        return len(matches) >= cfg.get("min_embedded_values", 2)

    def _try_extract_embedded_value(self, name: str, raw_result: str | None) -> tuple[str, float | None]:
        """Returns (name_to_match, extracted_value). extracted_value is None
        unless recovery fired."""
        cfg = self.matching["embedded_value_extraction"]
        if not cfg.get("enabled"):
            return name, None
        if cfg.get("use_when_result_empty") and raw_result not in (None, "", "N/A", "NA"):
            return name, None
        for pattern in cfg.get("patterns", []):
            m = re.match(pattern, name.strip())
            if m and "value" in m.groupdict():
                try:
                    value = float(m.group("value"))
                except (TypeError, ValueError):
                    continue
                return m.group("name").strip(), value
        return name, None

    def normalise(self, raw_name: str, raw_result: str | None = None) -> NormalisationResult:
        raw_name = raw_name or ""
        flags: list[str] = []

        is_composite = self._detect_composite(raw_name)
        if is_composite:
            flags.append(self.matching["composite_row_detection"]["flag"])

        extracted_value = None
        name_to_match = raw_name
        if not is_composite:
            name_to_match, extracted_value = self._try_extract_embedded_value(raw_name, raw_result)
            if extracted_value is not None:
                flags.append(self.matching["embedded_value_extraction"]["flag"])

        preprocessed = self.preprocess(name_to_match)
        preprocessed_lower = preprocessed.lower()

        is_panel_header = preprocessed_lower in self.known_panels
        if is_panel_header and self.panel_suppress_empty and raw_result not in (None, ""):
            # It has a real result — probably not actually a header row.
            is_panel_header = False

        canonical: str | None
        method: str
        confidence: float
        is_qualitative = False
        is_narrative = False
        best_candidate: str | None = None

        # --- exact match --------------------------------------------------
        if preprocessed_lower in self.clinical_lookup:
            canonical, method, confidence = self.clinical_lookup[preprocessed_lower], "exact", 1.0
        elif preprocessed_lower in self.qualitative_lookup:
            canonical, method, confidence = self.qualitative_lookup[preprocessed_lower], "exact", 1.0
            is_qualitative = True
        elif preprocessed_lower in self.narrative_set:
            canonical, method, confidence = preprocessed.upper(), "exact", 1.0
            is_narrative = True
        else:
            # --- fuzzy match ------------------------------------------------
            best = None
            if self.fuzzy_pool:
                scorer = getattr(fuzz, self.matching.get("fuzzy_scorer", "token_set_ratio"))
                best = process.extractOne(preprocessed_lower, self.fuzzy_pool.keys(), scorer=scorer)

            threshold = self.matching.get("fuzzy_threshold", 85)
            if best is not None and best[1] >= threshold:
                matched_variant, score, _ = best
                canonical, catalog = self.fuzzy_pool[matched_variant]
                method, confidence = "fuzzy", round(score / 100, 4)
                is_qualitative = catalog == "qualitative"
                is_narrative = catalog == "narrative"
            else:
                canonical, method, confidence = None, "unresolved", round((best[1] if best else 0.0) / 100, 4)
                best_candidate = best[0] if best else None
                flags = flags + ["unresolved_test_name"]

        # Value recovery is a distinct, fixed-confidence tier that overrides
        # whatever tier resolved the leftover name fragment (see
        # test_dictionary.yaml > matching.embedded_value_extraction).
        if extracted_value is not None:
            method, confidence = "extracted", 0.70

        return NormalisationResult(
            test_name_original=raw_name, test_name_canonical=canonical,
            normalization_method=method, normalization_confidence=confidence,
            extracted_value=extracted_value, is_qualitative=is_qualitative,
            is_narrative=is_narrative, is_panel_header=is_panel_header,
            is_composite_row=is_composite, best_candidate=best_candidate, flags=flags,
        )

    def canonical_unit_for(self, canonical_test_name: str | None) -> str | None:
        return self.clinical_units.get(canonical_test_name)
