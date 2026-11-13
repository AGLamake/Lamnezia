# Load Celery with Django so that @shared_task binds to this app.
from config.celery import app as celery_app

__all__ = ["celery_app"]
