"""Root URL configuration.

Every business endpoint is namespaced under ``/api/v1/`` so that a v2 can be
introduced alongside v1 rather than breaking existing mobile clients.
"""

from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from nuvix.core.views import HealthView, ReadinessView

api_v1 = [
    path("accounts/", include("nuvix.accounts.urls")),
    path("banking/", include("nuvix.banking.urls")),
    path("credit/", include("nuvix.credit.urls")),
    path("debt/", include("nuvix.debt.urls")),
    path("marketplace/", include("nuvix.marketplace.urls")),
    path("comms/", include("nuvix.comms.urls")),
    path("analytics/", include("nuvix.analytics.urls")),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", HealthView.as_view(), name="health"),
    path("readyz", ReadinessView.as_view(), name="readiness"),
    path("api/v1/", include((api_v1, "v1"))),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]
