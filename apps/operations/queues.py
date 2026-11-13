"""Celery queues: server polling runs apart from operations, so a long install does not delay it.

On a VPS with one CPU the worker runs one job at a time. Monitoring and traffic readings only
read the server and take no server lock, so they get their own queue and their own worker
(`poller` in compose); the main worker reads that queue too, so polling goes on without it.
"""

from typing import Any

DEFAULT_QUEUE = "celery"
POLL_QUEUE = "poll"

# Beat jobs that read the servers, and the per-server jobs they fan out.
POLL_TASKS = frozenset(
    {
        "servers.monitor",
        "servers.monitor_server",
        "traffic.collect",
        "traffic.collect_server",
    }
)


def route(name: str, args: Any, kwargs: Any, options: Any, task: Any = None, **kw: Any) -> Any:
    """`CELERY_TASK_ROUTES` router: polling jobs go to the poll queue, the rest stay default."""
    if name in POLL_TASKS:
        return {"queue": POLL_QUEUE}
    return None
