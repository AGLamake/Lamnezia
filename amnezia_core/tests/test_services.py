import pytest

from amnezia_core.errors import ConfigFormatError
from amnezia_core.services import SFTP_USER, SftpAccount, dns_logs_off, parse_onion


def test_generated_sftp_account_is_like_the_app_one() -> None:
    account = SftpAccount.generate()

    assert account.user == SFTP_USER
    assert len(account.password) == 16 and account.password.isalnum()
    assert account.argument == f"{SFTP_USER}:{account.password}:::upload"
    assert account.password not in repr(account)


def test_sftp_account_is_read_from_the_container_command() -> None:
    account = SftpAccount.from_command("[sftp_user:S3cret:::upload]\n")

    assert (account.user, account.password) == ("sftp_user", "S3cret")


@pytest.mark.parametrize("output", ["[]", "[sftp_user]", "[:pass:::upload]", ""])
def test_container_command_without_an_account_is_an_error(output: str) -> None:
    with pytest.raises(ConfigFormatError):
        SftpAccount.from_command(output)


def test_onion_address_is_read_from_the_hostname_file() -> None:
    assert parse_onion("abc.onion\n") == "abc.onion"


def test_empty_hostname_file_is_an_error() -> None:
    with pytest.raises(ConfigFormatError):
        parse_onion("\n")


def test_dns_logs_are_off_only_when_both_lines_say_so() -> None:
    assert dns_logs_off(" \nverbosity: 0\n  log-queries: no\nlog-replies: no\n")
    assert not dns_logs_off("log-queries: no\n")
    assert not dns_logs_off("forward-zone:\n   name: .\n")
