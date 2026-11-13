import factory

from apps.accounts.tests.factories import UserFactory
from apps.plans.models import Plan, UserLimits


class PlanFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Plan

    name = factory.Sequence(lambda n: f"Plan {n}")


class UserLimitsFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = UserLimits

    user = factory.SubFactory(UserFactory)
    plan = factory.SubFactory(PlanFactory)
