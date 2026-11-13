"""Celery application: background jobs (SSH operations, notifications) and the beat schedule."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("lamnezia")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
