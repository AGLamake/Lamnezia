from amnezia_core.docker_ps import RunningContainer, parse_docker_ps

OUTPUT = """\
amnezia-awg2 0.0.0.0:55424->55424/udp, [::]:55424->55424/udp
amnezia-xray 0.0.0.0:443->443/tcp, [::]:443->443/tcp
amnezia-dns 53/tcp, 53/udp
nginx 0.0.0.0:80->80/tcp
amnezia-sftp
"""


def test_finds_amnezia_containers_with_their_ports() -> None:
    assert parse_docker_ps(OUTPUT) == [
        RunningContainer("amnezia-awg2", 55424, "udp"),
        RunningContainer("amnezia-xray", 443, "tcp"),
        RunningContainer("amnezia-dns", 53, "tcp"),
    ]


def test_no_amnezia_containers() -> None:
    assert parse_docker_ps("") == []
    assert parse_docker_ps("nginx 0.0.0.0:80->80/tcp\n") == []
