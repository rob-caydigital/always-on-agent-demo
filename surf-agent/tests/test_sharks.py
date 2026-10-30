"""L1: shark threat rating (SPEC-shark-threat §5). No network, no LLM."""

from datetime import UTC, datetime, timedelta

import pytest

from surf_agent.models import SharkPing, SharkReport, SharkSighting
from surf_agent.sharks import (
    distance_factor,
    haversine_mi,
    match_species,
    qualifying_pings,
    rate_sharks,
    recency_factor,
    veto_rating,
)

RUN_AT = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
SPECIES = {
    "white": "White Shark (Carcharodon carcharias)",
    "tiger": "Tiger Shark (Galeocerdo cuvier)",
    "bull": "Bull Shark (Carcharhinus leucas)",
    "great_hammerhead": "Great Hammerhead (Sphyrna mokarran)",
    "blacktip": "Blacktip Shark (Carcharhinus limbatus)",
    "oceanic_whitetip": "Oceanic Whitetip (Carcharhinus longimanus)",
    "blue": "Blue Shark (Prionace glauca)",
    "whale": "Whale Shark (Rhincodon typus)",
}


@pytest.fixture(scope="module")
def scfg(cfg):
    assert cfg.sharks is not None, "config/sharks.yaml should load into ScoringConfig.sharks"
    return cfg.sharks


def report(*sharks: tuple[str, str | None, float, float]) -> SharkReport:
    """Each shark: (species key, stage, distance_mi, age_h)."""
    nearby = [
        SharkSighting(
            id=i, name=f"s{i}", species=SPECIES[sp], stage_of_life=stage, track_used=True,
            pings=[SharkPing(time=RUN_AT - timedelta(hours=age), lat=0, lon=0, distance_mi=dist, age_h=age)],
        )
        for i, (sp, stage, dist, age) in enumerate(sharks)
    ]
    return SharkReport(status="checked", nearby=nearby)


def test_haversine_one_degree():
    # 2πR/360 with R = 3958.7613 mi
    assert haversine_mi(0, 0, 1, 0) == pytest.approx(69.093, abs=0.01)
    assert haversine_mi(0, 0, 0, 1) == pytest.approx(69.093, abs=0.01)
    assert haversine_mi(26.8933, -80.0557, 26.8933, -80.0557) == 0


@pytest.mark.parametrize(
    "age_h,expected", [(0, 1.0), (6, 1.0), (6.01, 0.85), (12, 0.85), (12.01, 0.7), (24, 0.7), (24 + 1 / 60, None), (-1, None)]
)
def test_recency_boundaries(scfg, age_h, expected):
    assert recency_factor(age_h, scfg) == expected


@pytest.mark.parametrize(
    "dist,expected", [(0, 1.0), (3, 1.0), (3.01, 0.8), (6, 0.8), (6.01, 0.6), (10.0, 0.6), (10.01, None)]
)
def test_distance_boundaries(scfg, dist, expected):
    assert distance_factor(dist, scfg) == expected


@pytest.mark.parametrize(
    "species,key,weight",
    [
        ("White Shark (Carcharodon carcharias)", "Carcharodon carcharias", 1.0),
        ("Great White (carcharodon CARCHARIAS)", "Carcharodon carcharias", 1.0),  # scientific name wins
        ("Tiger Shark", "Galeocerdo cuvier", 1.0),  # alias only
        ("Hammerhead Shark (Sphyrnidae)", "Sphyrnidae", 0.5),
        ("Whale Shark (Rhincodon typus)", "Rhincodon typus", 0.0),
        ("Lemon Shark (Negaprion brevirostris)", None, 0.5),  # unknown species
    ],
)
def test_match_species(scfg, species, key, weight):
    assert match_species(species, scfg) == (key, weight)


# SPEC-shark-threat §5.5, row by row.
@pytest.mark.parametrize(
    "shark,threat,vetoes",
    [
        (("white", "Adult", 2, 6), 1.00, True),
        (("tiger", "Adult", 9, 20), 0.42, True),
        (("bull", "Young of the Year", 8, 18), 0.17, False),
        (("great_hammerhead", "Adult", 4, 10), 0.34, True),
        (("blacktip", "Juvenile", 9, 20), 0.13, False),
        (("oceanic_whitetip", "Adult", 1, 2), 0.50, True),
        (("blue", "Adult", 5, 10), 0.17, False),
        (("whale", None, 1, 2), 0.00, False),
    ],
)
def test_worked_examples(scfg, shark, threat, vetoes):
    ratings = rate_sharks(report(shark), scfg)
    assert ratings[0].threat == threat
    assert (veto_rating(ratings, scfg) is not None) is vetoes


def test_outside_radius_and_lookback_never_qualify(scfg):
    lat, lon = 26.8933, -80.0557
    one_mile_lat = 1 / 69.093
    points = [
        (RUN_AT - timedelta(hours=2), lat + 10.5 * one_mile_lat, lon),  # 10.5 mi: outside radius
        (RUN_AT - timedelta(hours=30), lat + 2 * one_mile_lat, lon),  # 30 h: outside lookback
        (RUN_AT - timedelta(hours=3), lat + 2 * one_mile_lat, lon),  # qualifies
        (RUN_AT - timedelta(hours=3), lat + 2 * one_mile_lat, lon),  # duplicate ping
    ]
    pings = qualifying_pings(points, lat, lon, RUN_AT, scfg)
    assert len(pings) == 1
    assert pings[0].distance_mi == pytest.approx(2, abs=0.01)
    assert pings[0].age_h == 3


def test_worst_ping_wins_and_sorted(scfg):
    rep = report(("blue", "Adult", 5, 10), ("white", "Adult", 9, 20))
    rep.nearby[1].pings.append(
        SharkPing(time=RUN_AT - timedelta(hours=1), lat=0, lon=0, distance_mi=2, age_h=1)
    )
    ratings = rate_sharks(rep, scfg)
    assert [r.name for r in ratings] == ["s1", "s0"]
    assert ratings[0].threat == 1.0 and ratings[0].worst_ping.distance_mi == 2


def test_threshold_compares_rounded_threat(scfg):
    # 0.5 × 0.8 × 0.85 × 0.6 = 0.204 -> 0.2, which vetoes at exactly the threshold
    ratings = rate_sharks(report(("blacktip", "Sub-Adult", 9, 10)), scfg)
    assert ratings[0].threat == 0.2
    assert veto_rating(ratings, scfg) is not None


@pytest.mark.parametrize("status", ["unavailable", "not_captured", "disabled"])
def test_unchecked_reports_rate_nothing(scfg, status):
    assert rate_sharks(SharkReport(status=status), scfg) == []
    assert rate_sharks(None, scfg) == []
