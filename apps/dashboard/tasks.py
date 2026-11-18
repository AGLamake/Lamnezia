"""Beat and on-demand jobs of the dashboard: the scheduled report to the staff."""

from celery import shared_task
from django.utils import timezone

from apps.dashboard.services.report import StaffReports


@shared_task(name="dashboard.send_reports")
def send_reports() -> int:
    return StaffReports.from_settings(timezone.now()).send_due()


@shared_task(name="dashboard.send_reports_now")
def send_reports_now() -> int:
    return StaffReports.from_settings(timezone.now()).send_now()
