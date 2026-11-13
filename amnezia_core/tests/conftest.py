from collections.abc import Callable
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# Peers of fixtures/awg0.conf, rendered from upstream configure_container.sh.
FIRST_PEER = "jhkQdvDcgDYudvey3hItwkaQoDy/bLT6QnsyYHwCIlo="
SECOND_PEER = "Q2+/5jIlpVjLMKDArTagVYSPsI5fcqPNpSem9NMi50s="


@pytest.fixture
def read_fixture() -> Callable[[str], str]:
    def read(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return read
