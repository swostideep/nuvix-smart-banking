from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "nuvix.accounts"

    def ready(self) -> None:
        from nuvix.accounts import signals  # noqa: F401
