from django.contrib.auth import views as auth_views
from django.urls import path

from apps.accounts import views, views_admin, views_links, views_profile, views_registration
from apps.accounts.models import Messenger

app_name = "accounts"

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("profile/", views_profile.ProfileView.as_view(), name="profile"),
    path("profile/email/", views_profile.EmailChangeView.as_view(), name="email-change"),
    path(
        "profile/email/<str:token>/",
        views_profile.EmailConfirmView.as_view(),
        name="email-confirm",
    ),
    path("profile/notifications/", views_profile.NotificationsView.as_view(), name="notifications"),
    path("profile/sessions/end/", views_profile.SessionEndView.as_view(), name="session-end"),
    path("security/", views_profile.SectionRedirect.as_view(section="two-factor"), name="security"),
    path("password/", views_profile.PasswordChangeView.as_view(), name="password-change"),
    path("password/reset/", views.PasswordResetView.as_view(), name="password-reset"),
    path(
        "password/reset/sent/",
        auth_views.PasswordResetDoneView.as_view(template_name="accounts/password_reset_sent.html"),
        name="password-reset-sent",
    ),
    path(
        "password/reset/<uidb64>/<token>/",
        views.PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    path("2fa/verify/", views.TwoFactorVerifyView.as_view(), name="two-factor-verify"),
    path("2fa/setup/", views.TwoFactorSetupView.as_view(), name="two-factor-setup"),
    path("2fa/disable/", views.TwoFactorDisableView.as_view(), name="two-factor-disable"),
    path("2fa/backup-codes/", views.BackupCodesView.as_view(), name="backup-codes"),
    path("register/", views_registration.RegisterView.as_view(), name="register"),
    path("invite/<str:code>/", views_registration.RegisterView.as_view(), name="invite"),
    path(
        "confirm/<uidb64>/<token>/",
        views_registration.ConfirmEmailView.as_view(),
        name="confirm-email",
    ),
    path("link/", views_registration.MagicLinkRequestView.as_view(), name="magic-link"),
    path("link/<str:token>/", views_registration.MagicLoginView.as_view(), name="magic-login"),
    path("registrations/", views_admin.RegistrationsView.as_view(), name="registrations"),
    path(
        "registrations/<int:pk>/approve/",
        views_admin.ApproveView.as_view(),
        name="registration-approve",
    ),
    path(
        "registrations/<int:pk>/reject/",
        views_admin.RejectView.as_view(),
        name="registration-reject",
    ),
    path("messengers/", views_profile.SectionRedirect.as_view(section="messengers"), name="linked"),
    path(
        "messengers/offer/dismiss/",
        views_links.LinkOfferDismissView.as_view(),
        name="link-offer-dismiss",
    ),
    path(
        "telegram/code/",
        views_profile.LinkCodeView.as_view(messenger=Messenger.TELEGRAM),
        name="telegram-code",
    ),
    path(
        "telegram/unlink/",
        views_links.UnlinkView.as_view(messenger=Messenger.TELEGRAM),
        name="telegram-unlink",
    ),
    path("telegram/login/", views_links.TelegramLoginView.as_view(), name="telegram-login"),
    path(
        "discord/code/",
        views_profile.LinkCodeView.as_view(messenger=Messenger.DISCORD),
        name="discord-code",
    ),
    path(
        "discord/unlink/",
        views_links.UnlinkView.as_view(messenger=Messenger.DISCORD),
        name="discord-unlink",
    ),
    path("discord/connect/", views_links.DiscordConnectView.as_view(), name="discord-connect"),
    path(
        "discord/login/",
        views_links.DiscordLoginView.as_view(),
        name="discord-login",
    ),
    path("discord/callback/", views_links.DiscordCallbackView.as_view(), name="discord-callback"),
    path("invites/add/", views_admin.InviteCreateView.as_view(), name="invite-add"),
    path("invites/<int:pk>/delete/", views_admin.InviteDeleteView.as_view(), name="invite-delete"),
]
