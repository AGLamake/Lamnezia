import pytest

from apps.ssh.errors import ERROR_CATALOG, ErrorCode, SshConnectionError


@pytest.mark.parametrize("code", list(ErrorCode))
def test_every_error_has_message_and_hint(code: ErrorCode) -> None:
    error = SshConnectionError(code)

    assert error.message.strip()
    assert error.hint.strip()


def test_catalog_has_no_unknown_codes() -> None:
    assert set(ERROR_CATALOG) == set(ErrorCode)


def test_detail_stays_out_of_user_message() -> None:
    error = SshConnectionError(ErrorCode.AUTH_FAILED, "paramiko: Authentication failed.")

    assert "paramiko" not in error.message
    assert "paramiko" in str(error)
