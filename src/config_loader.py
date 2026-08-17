"""Loads the four YAML config files once and exposes them as plain dicts.

Every mapping decision in the pipeline is driven by these files (NFR-2.1) —
no module should ever hardcode a test name, unit, or field map.
"""
from __future__ import annotations

import functools
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _load(name: str) -> dict:
    path = CONFIG_DIR / name
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@functools.lru_cache(maxsize=1)
def get_document_type_mappings() -> dict:
    return _load("document_type_mappings.yaml")


@functools.lru_cache(maxsize=1)
def get_test_dictionary() -> dict:
    return _load("test_dictionary.yaml")


@functools.lru_cache(maxsize=1)
def get_reference_ranges() -> dict:
    return _load("reference_ranges.yaml")


@functools.lru_cache(maxsize=1)
def get_unit_conversions() -> dict:
    return _load("unit_conversions.yaml")


def reload_all() -> None:
    """Clears cached config — used by tests that swap in synthetic config."""
    get_document_type_mappings.cache_clear()
    get_test_dictionary.cache_clear()
    get_reference_ranges.cache_clear()
    get_unit_conversions.cache_clear()
