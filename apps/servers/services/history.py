from apps.operations.models import Operation
from apps.servers.models import Server


def latest_operation(server: Server) -> Operation | None:
    """The newest operation on the server of any kind; server operations name it in params."""
    operations = Operation.objects.filter(params__server_id=server.pk)
    return operations.order_by("-created_at", "-pk").first()
