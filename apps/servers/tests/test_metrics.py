from apps.servers.services.metrics import ContainerState, CpuTimes, HostMetrics, parse_metrics

OUTPUT = """\
@@lamnezia-stat
cpu  100 0 50 800 50 0 0 0 30 0
@@lamnezia-meminfo
MemTotal:        2000000 kB
MemFree:          300000 kB
MemAvailable:     500000 kB
@@lamnezia-uptime
86461.42 170000.10
@@lamnezia-disk
20000000 15000000
@@lamnezia-containers
amnezia-awg2 running
amnezia-xray exited
portainer running
lamnezia-probe exited
"""


def test_a_full_reading_is_parsed() -> None:
    metrics = parse_metrics(OUTPUT)

    assert metrics == HostMetrics(
        cpu=CpuTimes(total=1000, idle=850),
        memory_total=2_048_000_000,
        memory_available=512_000_000,
        disk_total=20_480_000_000,
        disk_free=15_360_000_000,
        uptime=86461,
        containers=(
            ContainerState("amnezia-awg2", "running"),
            ContainerState("amnezia-xray", "exited"),
            ContainerState("lamnezia-probe", "exited"),
        ),
    )


def test_values_the_server_did_not_report_stay_unknown() -> None:
    output = "@@lamnezia-stat\n@@lamnezia-disk\nsomething odd\n@@lamnezia-containers\n"

    assert parse_metrics(output) == HostMetrics()


def test_cpu_load_is_the_busy_share_between_two_readings() -> None:
    earlier = CpuTimes(total=1000, idle=850)

    assert CpuTimes(total=1200, idle=900).busy_percent_since(earlier) == 75.0


def test_cpu_load_is_unknown_after_a_reboot() -> None:
    earlier = CpuTimes(total=1000, idle=850)

    assert CpuTimes(total=200, idle=150).busy_percent_since(earlier) is None
