import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from celery import Celery
from celery.contrib.testing.worker import start_worker
from django.conf import settings

from apps.operations.queues import DEFAULT_QUEUE, POLL_QUEUE, POLL_TASKS
from config.celery import app as panel_app

COMPOSE = Path(__file__).resolve().parents[3] / "compose.yaml"
WAIT = 10


def queue_of(name: str) -> str:
    route = panel_app.amqp.router.route({}, name)
    return str(route["queue"].name)


@pytest.mark.parametrize("name", sorted(POLL_TASKS))
def test_polling_goes_to_its_own_queue(name: str) -> None:
    assert queue_of(name) == POLL_QUEUE


@pytest.mark.parametrize("name", ["operations.run", "keys.reconcile", "traffic.enforce_limits"])
def test_other_jobs_stay_in_the_default_queue(name: str) -> None:
    assert queue_of(name) == DEFAULT_QUEUE


def test_every_polling_beat_job_is_routed_to_the_poll_queue() -> None:
    tasks = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}
    assert {"servers.monitor", "traffic.collect"} <= tasks
    assert all(queue_of(task) == POLL_QUEUE for task in tasks & POLL_TASKS)


def test_compose_runs_a_worker_for_the_poll_queue() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    assert "worker -Q celery,poll " in compose, "the main worker reads both queues"
    assert "worker -Q poll -n poll@%h" in compose


@pytest.fixture
def queues_app() -> Iterator[Celery]:
    """An app with the panel's routes over an in-memory broker, run by real workers."""
    app = Celery("queues-test", broker="memory://", set_as_current=False)
    app.conf.update(
        task_routes=settings.CELERY_TASK_ROUTES,
        worker_prefetch_multiplier=1,
        task_ignore_result=True,
        broker_connection_retry_on_startup=True,
    )
    yield app
    app.close()


def test_polling_runs_while_a_long_operation_holds_the_worker(queues_app: Celery) -> None:
    """The main worker runs one job at a time, as on a VPS with one CPU."""
    installing, install_may_end, polled = threading.Event(), threading.Event(), threading.Event()

    @queues_app.task(name="operations.run", shared=False, lazy=False)
    def install() -> None:
        installing.set()
        install_may_end.wait(WAIT)

    @queues_app.task(name="traffic.collect", shared=False, lazy=False)
    def collect() -> None:
        polled.set()

    main = start_worker(
        queues_app,
        queues=[DEFAULT_QUEUE],
        concurrency=1,
        perform_ping_check=False,
        shutdown_timeout=WAIT,
    )
    poller = start_worker(
        queues_app,
        queues=[POLL_QUEUE],
        concurrency=1,
        perform_ping_check=False,
        shutdown_timeout=WAIT,
    )
    with main, poller:
        try:
            queues_app.send_task(install.name)
            assert installing.wait(WAIT)
            queues_app.send_task(collect.name)
            assert polled.wait(WAIT), "the reading waited for the install"
            assert not install_may_end.is_set()
        finally:
            install_may_end.set()
