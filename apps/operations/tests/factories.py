import factory

from apps.operations.models import Operation, OperationStatus, OperationStep, StepStatus


class OperationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Operation

    kind = "test.noop"
    language = "en"


class FailedOperationFactory(OperationFactory):
    status = OperationStatus.FAILED
    error_code = "command_failed"
    error_message = "A command on the server failed."
    error_hint = "See the operation log for details."


class OperationStepFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = OperationStep

    operation = factory.SubFactory(OperationFactory)
    position = factory.Sequence(lambda n: n + 1)
    title = "Connect to the server"
    status = StepStatus.SUCCEEDED
