from celery import shared_task
from django.core.mail import EmailMultiAlternatives


# SMTPException and connection errors are OSError: a mail server that is down gets retried.
@shared_task(
    name="accounts.send_letter", autoretry_for=(OSError,), retry_backoff=True, max_retries=5
)
def send_letter(subject: str, body: str, to: list[str], html: str = "") -> None:
    """Plain text, with the HTML version alongside when there is one."""
    letter = EmailMultiAlternatives(subject, body, None, to)
    if html:
        letter.attach_alternative(html, "text/html")
    letter.send()
