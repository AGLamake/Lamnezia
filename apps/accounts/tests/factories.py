import factory
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import User


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User

    username = factory.Sequence(lambda n: f"user{n}")
    email = factory.LazyAttribute(lambda user: f"{user.username}@example.com")
    password = factory.django.Password("password")


class TOTPDeviceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = TOTPDevice

    user = factory.SubFactory(UserFactory)
    name = "default"
    confirmed = True


def current_code(device: TOTPDevice) -> str:
    """The code the authenticator app shows for `device` right now."""
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    return f"{totp.token():0{device.digits}d}"
