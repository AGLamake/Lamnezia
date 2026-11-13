"""Operation kinds of the support app; registered in `SupportConfig.ready()`."""

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.operations.services.context import OperationContext
from apps.operations.services.registry import operations
from apps.support.models import Conversation
from apps.support.services.check import CHECK_KIND, RequestServerCheck


@operations.register(CHECK_KIND, gettext_lazy("Check a server for a request"))
def check_server(ctx: OperationContext) -> None:
    conversation = Conversation.objects.select_related("server", "user").get(
        pk=ctx.params["conversation_id"]
    )
    if conversation.server is None:
        ctx.log(_("The server is deleted."))
        return
    with ctx.step(_("Check the server")):
        report = RequestServerCheck().run(conversation, conversation.server)
        ctx.log(_("SSH answers.") if report.answered else _("SSH does not answer."))
