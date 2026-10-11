"""Versioned, dependency-free claims ledger validation."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .store import fold_for_match


@dataclass(frozen=True)
class Claim:
    id: str
    text: str
    kind: str
    importance: str
    evidence: tuple[str, ...]

    @property
    def requires_full_evidence(self) -> bool:
        return self.importance == "core" and self.kind in ("fact", "inference")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def load_claims(ledger: dict[str, Any] | str | os.PathLike[str]) -> list[Claim]:
    """Accept a parsed ledger or a UTF-8 JSON file; reject incomplete ledgers."""
    if isinstance(ledger, (str, os.PathLike)):
        try:
            with open(ledger, encoding="utf-8-sig") as handle:
                ledger = json.load(
                    handle, object_pairs_hook=_unique_object,
                    parse_constant=_invalid_constant,
                )
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError(f"Cannot read claims ledger: {exc}") from exc
    if not isinstance(ledger, dict):
        raise ValueError("Claims ledger must be a JSON object")
    version = ledger.get("schema_version")
    if type(version) is not int or version != 1:
        raise ValueError("Claims ledger requires schema_version=1")
    rows = ledger.get("claims")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Claims ledger requires a nonempty claims list")

    claims: list[Claim] = []
    seen: set[str] = set()
    required = {"id", "text", "kind", "importance", "evidence"}
    for position, row in enumerate(rows, 1):
        label = f"claims[{position}]"
        if not isinstance(row, dict) or not required.issubset(row):
            raise ValueError(f"{label} requires id, text, kind, importance, evidence")
        for key in ("id", "text"):
            if not isinstance(row[key], str) or not fold_for_match(row[key]):
                raise ValueError(f"{label}.{key} must be a nonempty string")
        claim_id = row["id"].strip()
        if claim_id in seen:
            raise ValueError(f"Duplicate claim id: {claim_id}")
        seen.add(claim_id)
        if row["kind"] not in ("fact", "inference", "assumption", "company"):
            raise ValueError(f"{label}.kind must be fact/inference/assumption/company")
        if row["importance"] not in ("core", "supporting"):
            raise ValueError(f"{label}.importance must be core/supporting")
        evidence = row["evidence"]
        if not isinstance(evidence, list):
            raise ValueError(f"{label}.evidence must be a URL list")
        for url in evidence:
            if not isinstance(url, str) or any(c.isspace() for c in url):
                raise ValueError(f"{label}.evidence contains an invalid URL")
            try:
                parts = urlsplit(url)
                valid = parts.scheme in ("http", "https") and bool(parts.hostname)
            except ValueError:
                valid = False
            if not valid:
                raise ValueError(f"{label}.evidence contains an invalid HTTP(S) URL")
        if len(set(evidence)) != len(evidence):
            raise ValueError(f"{label}.evidence contains duplicate URLs")
        claims.append(Claim(claim_id, row["text"], row["kind"], row["importance"], tuple(evidence)))
    return claims
