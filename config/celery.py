"""Celery application: background jobs (SSH operations, notifications) and the beat schedule."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Every task pauses while the panel moves to another machine (apps/relocation).
app = Celery("lamnezia", task_cls="apps.relocation.tasks:PanelTask")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
