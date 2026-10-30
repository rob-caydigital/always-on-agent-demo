from pathlib import Path

import pytest

from surf_agent.config import load_scoring, load_settings, load_spot


@pytest.fixture(scope="session")
def settings():
    return load_settings()


@pytest.fixture(scope="session")
def prefs(settings):
    return settings.preferences


@pytest.fixture(scope="session")
def cfg():
    return load_scoring()


@pytest.fixture(scope="session")
def spot():
    return load_spot("juno-beach-pier")


FIXTURES = Path(__file__).parent / "fixtures"
