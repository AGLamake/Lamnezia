from django.urls import path

from apps.support import views_inbox, views_templates
from apps.support.views import (
    AttachmentView,
    ChatUnreadView,
    ChatView,
    CheckStateView,
    ConversationView,
    MessagesView,
    RequestCreateView,
    RequestListView,
    ServerDownView,
    UnreadView,
)

app_name = "support"

urlpatterns = [
    path("", RequestListView.as_view(), name="index"),
    path("new/", RequestCreateView.as_view(), name="new"),
    path("server-down/", ServerDownView.as_view(), name="server-down"),
    path("<int:pk>/", ConversationView.as_view(), name="conversation"),
    path("<int:pk>/check/", CheckStateView.as_view(), name="check"),
    path("<int:pk>/messages/", MessagesView.as_view(), name="messages"),
    path("chat/", ChatView.as_view(), name="chat"),
    path("chat/unread/", ChatUnreadView.as_view(), name="chat-unread"),
    path("unread/", UnreadView.as_view(), name="unread"),
    path("attachments/<int:pk>/", AttachmentView.as_view(), name="attachment"),
    path("inbox/", views_inbox.RequestListView.as_view(), name="inbox"),
    path("inbox/chats/", views_inbox.ChatListView.as_view(), name="inbox-chats"),
    path("inbox/<int:pk>/", views_inbox.ConversationView.as_view(), name="inbox-conversation"),
    path("inbox/<int:pk>/messages/", views_inbox.MessagesView.as_view(), name="inbox-messages"),
    path("inbox/unread/", views_inbox.UnreadView.as_view(), name="inbox-unread"),
    path("inbox/<int:pk>/manage/", views_inbox.ManageView.as_view(), name="inbox-manage"),
    path("inbox/incidents/", views_inbox.IncidentListView.as_view(), name="incidents"),
    path("inbox/templates/", views_templates.TemplateListView.as_view(), name="templates"),
    path("inbox/templates/add/", views_templates.TemplateCreateView.as_view(), name="template-add"),
    path(
        "inbox/templates/<int:pk>/",
        views_templates.TemplateUpdateView.as_view(),
        name="template-edit",
    ),
    path(
        "inbox/templates/<int:pk>/delete/",
        views_templates.TemplateDeleteView.as_view(),
        name="template-delete",
    ),
    path(
        "inbox/incidents/<int:pk>/close/",
        views_inbox.IncidentCloseView.as_view(),
        name="incident-close",
    ),
]
