"""Dealer registry loader.

`data/dealer_master.csv` is the source of truth for which dealers the scraper
runs against. This module defines the row schema (`DealerConfig`) and the
loader (`load_registry`) used by every other slice.
"""

from __future__ import annotations

import csv
import json
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator


class Platform(StrEnum):
    XTIME = "xtime"
    MYKAARMA = "mykaarma"
    DEALER_FX = "dealer_fx"
    CONNECT_CDK = "connect_cdk"
    CUSTOM = "custom"
    UNKNOWN = "unknown"


class RegistryError(Exception):
    """Raised when the registry CSV fails to load or validate."""


class DealerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    dealer_code: str
    dealer_name: str
    dealer_url: str
    schedule_url: str
    platform: Platform
    zip: str
    region: str
    config_json: dict[str, Any]
    active: bool
    notes: str

    @field_validator("dealer_code", "dealer_name", mode="after")
    @classmethod
    def _required_non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("config_json", mode="before")
    @classmethod
    def _parse_config_json(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return value
        if value is None or value == "":
            return {}
        if not isinstance(value, str):
            raise ValueError("config_json must be a JSON object string")
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"config_json is not valid JSON: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("config_json must decode to a JSON object")
        return parsed


def load_registry(path: Path) -> list[DealerConfig]:
    """Read the dealer registry CSV and return active dealers.

    Raises `RegistryError` on any malformed row — the registry is source of
    truth, so silent skips would mask data-quality bugs (SPEC.md principle #6).
    """
    dealers: list[DealerConfig] = []
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for index, row in enumerate(reader, start=2):  # header is row 1
            try:
                dealer = DealerConfig.model_validate(row)
            except ValidationError as exc:
                raise RegistryError(
                    f"{path}:row {index}: invalid dealer row: {exc}"
                ) from exc
            if dealer.active:
                dealers.append(dealer)
    return dealers
