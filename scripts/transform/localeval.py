#!/usr/bin/env python3
"""Unwrap a Contentful field value for one locale (deterministic).

Entries exported with locale=* store EVERY field as {locale: value}. A field with no value in the configured locale
(for example only {"fr": "..."}) must read as "no value", never as the whole locale map: the old fall-through would have
written {"fr": ...} into a Builder text field. Already-flat values (e.g. a JSON object field in a flat test entry) pass
through unchanged: a dict is treated as a locale map only if every key looks like a locale code.
"""
from __future__ import annotations

import re

LOCALE_KEY = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


def is_locale_map(value) -> bool:
    return isinstance(value, dict) and bool(value) and all(isinstance(k, str) and LOCALE_KEY.match(k) for k in value)


def unwrap(value, locale: str):
    if is_locale_map(value):
        return value.get(locale)
    return value
