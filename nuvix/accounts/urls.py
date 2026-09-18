from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView, TokenVerifyView

from nuvix.accounts.views import (
    ChangePasswordView,
    LoginView,
    MeView,
    ProfileView,
    RegistrationView,
)

app_name = "accounts"

urlpatterns = [
    path("register", RegistrationView.as_view(), name="register"),
    path("login", LoginView.as_view(), name="login"),
    path("token/refresh", TokenRefreshView.as_view(), name="token-refresh"),
    path("token/verify", TokenVerifyView.as_view(), name="token-verify"),
    path("me", MeView.as_view(), name="me"),
    path("me/profile", ProfileView.as_view(), name="profile"),
    path("me/password", ChangePasswordView.as_view(), name="change-password"),
]
