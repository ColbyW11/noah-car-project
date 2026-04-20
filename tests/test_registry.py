"""Tests for vw_scraper.registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from vw_scraper.registry import (
    DealerConfig,
    Platform,
    RegistryError,
    load_registry,
)

FIXTURES = Path(__file__).parent / "fixtures" / "registry"
STARTER_CSV = Path(__file__).parent.parent / "data" / "dealer_master.csv"


def test_load_registry_returns_active_dealers_from_starter_csv() -> None:
    dealers = load_registry(STARTER_CSV)

    assert len(dealers) == 5
    assert [d.dealer_code for d in dealers] == [
        "VW0001",
        "VW0002",
        "VW0003",
        "VW0004",
        "VW0005",
    ]
    assert all(d.platform is Platform.UNKNOWN for d in dealers)
    assert all(d.active for d in dealers)
    assert all(d.config_json == {} for d in dealers)


def test_load_registry_parses_valid_csv() -> None:
    dealers = load_registry(FIXTURES / "valid.csv")

    assert len(dealers) == 2

    first = dealers[0]
    assert isinstance(first, DealerConfig)
    assert first.dealer_code == "VW1001"
    assert first.platform is Platform.XTIME
    assert first.config_json == {"vehicle": "Jetta", "requires_zip": True}

    assert dealers[1].platform is Platform.MYKAARMA
    assert dealers[1].config_json == {}


def test_load_registry_raises_on_invalid_platform() -> None:
    with pytest.raises(RegistryError) as exc:
        load_registry(FIXTURES / "invalid_platform.csv")

    message = str(exc.value)
    assert "row 2" in message
    assert "platform" in message


def test_load_registry_raises_on_malformed_config_json() -> None:
    with pytest.raises(RegistryError) as exc:
        load_registry(FIXTURES / "malformed_config.csv")

    message = str(exc.value)
    assert "row 2" in message
    assert "config_json" in message


def test_load_registry_filters_inactive_rows() -> None:
    dealers = load_registry(FIXTURES / "mixed_active.csv")

    assert len(dealers) == 3
    assert [d.dealer_code for d in dealers] == ["VW4001", "VW4003", "VW4005"]
    assert all(d.active for d in dealers)
