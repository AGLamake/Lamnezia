from collections.abc import Iterator

import pytest
from django.core.cache import cache

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.api.errors import (
    TokenInvalidError,
    TokenMissingError,
    TokenRejectedError,
    TooManyCallsError,
)
from apps.api.services.gate import ApiGate, ApiThrottle
from apps.api.services.tokens import ApiTokens, IssuedToken

pytestmark = pytest.mark.django_db

IP = "198.51.100.7"


@pytest.fixture(autouse=True)
def _empty_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def issued() -> IssuedToken:
    admin = UserFactory(role=Role.ADMIN)
    return ApiTokens().issue(name="monitoring", owner=admin, by=admin)


def test_a_working_token_is_let_in(issued: IssuedToken) -> None:
    assert ApiGate(limit=5).admit(f"Bearer {issued.value}", IP) == issued.token


def test_the_scheme_is_case_insensitive(issued: IssuedToken) -> None:
    assert ApiGate(limit=5).admit(f"bearer  {issued.value} ", IP) == issued.token


@pytest.mark.parametrize("header", ["", "Bearer", "Bearer   ", "Basic dXNlcjpwYXNz", "lmz_x"])
def test_a_call_without_a_bearer_token_is_refused(header: str) -> None:
    with pytest.raises(TokenMissingError):
        ApiGate(limit=5).admit(header, IP)


def test_an_unknown_token_is_refused() -> None:
    with pytest.raises(TokenInvalidError):
        ApiGate(limit=5).admit("Bearer lmz_unknown", IP)


def test_a_disabled_token_is_refused_with_the_token_for_the_audit(issued: IssuedToken) -> None:
    ApiTokens().deactivate(issued.token)

    with pytest.raises(TokenRejectedError) as refused:
        ApiGate(limit=5).admit(f"Bearer {issued.value}", IP)
    assert refused.value.token == issued.token


def test_calls_over_the_limit_of_a_token_wait_for_the_next_minute(issued: IssuedToken) -> None:
    gate = ApiGate(limit=2)
    gate.admit(f"Bearer {issued.value}", IP)
    gate.admit(f"Bearer {issued.value}", "203.0.113.9")

    with pytest.raises(TooManyCallsError) as refused:
        gate.admit(f"Bearer {issued.value}", IP)
    assert refused.value.headers() == {"Retry-After": str(ApiThrottle.WINDOW_SECONDS)}


def test_an_address_guessing_tokens_is_stopped_before_the_lookup(issued: IssuedToken) -> None:
    gate = ApiGate(limit=2)
    for _attempt in range(2):
        with pytest.raises(TokenInvalidError):
            gate.admit("Bearer lmz_guess", IP)

    with pytest.raises(TooManyCallsError):
        gate.admit(f"Bearer {issued.value}", IP)
    assert gate.admit(f"Bearer {issued.value}", "203.0.113.9") == issued.token


def test_no_limit_lets_every_call_in(issued: IssuedToken) -> None:
    gate = ApiGate(limit=0)
    for _call in range(20):
        gate.admit(f"Bearer {issued.value}", IP)
