"""Shared dataclass models used across scrapers and storage.

`ScrapeResult` is the in-memory shape of a single dealer's scrape outcome and
must round-trip cleanly to the JSON observation record schema in SPEC.md
(lines 53–71). Slice 5's JSONL writer will call `model_dump_json` on this.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from . import __version__
from .registry import Platform


class ScrapeStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"


class ScrapeResult(BaseModel):
    """One dealer's scrape outcome.

    SPEC.md calls for a "dataclass" here; we use Pydantic for consistency with
    `DealerConfig` and to get JSON serialization for the Slice 5 JSONL writer.
    Field names match the observation record JSON exactly.
    """

    model_config = ConfigDict(extra="forbid")

    dealer_code: str
    observation_ts: datetime
    scrape_status: ScrapeStatus
    error_message: str | None
    first_available_ts: datetime | None
    lead_time_hours: float | None
    available_slots: list[datetime]
    slot_count: int
    scheduling_flow_seconds: float | None
    interaction_steps: int
    platform: Platform
    source_payload_hash: str | None
    scraper_version: str = __version__

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        if self.scrape_status is ScrapeStatus.SUCCESS and self.error_message is not None:
            raise ValueError("error_message must be None when scrape_status='success'")
        if self.scrape_status is ScrapeStatus.ERROR and self.error_message is None:
            raise ValueError("error_message is required when scrape_status='error'")
        if self.slot_count != len(self.available_slots):
            raise ValueError(
                f"slot_count={self.slot_count} does not match "
                f"len(available_slots)={len(self.available_slots)}"
            )
        if self.first_available_ts is None and self.available_slots:
            raise ValueError("first_available_ts must be set when slots exist")
        if self.observation_ts.tzinfo is None:
            raise ValueError("observation_ts must be timezone-aware (UTC)")
        return self
