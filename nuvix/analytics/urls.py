from django.urls import path

from nuvix.analytics.views import (
    AttributionView,
    CampaignPaybackView,
    ChannelPerformanceView,
    CohortRetentionView,
    FunnelEventCreateView,
    FunnelView,
    MarketingSpendViewSet,
    SubscriptionView,
    TouchPointCreateView,
)

app_name = "analytics"

urlpatterns = [
    # Reporting (staff only)
    path("reports/channels", ChannelPerformanceView.as_view(), name="report-channels"),
    path("reports/retention", CohortRetentionView.as_view(), name="report-retention"),
    path("reports/funnel", FunnelView.as_view(), name="report-funnel"),
    path("reports/attribution", AttributionView.as_view(), name="report-attribution"),
    path("reports/payback", CampaignPaybackView.as_view(), name="report-payback"),
    # Ingestion
    path("touchpoints", TouchPointCreateView.as_view(), name="touchpoint-create"),
    path("events", FunnelEventCreateView.as_view(), name="event-create"),
    path("spend", MarketingSpendViewSet.as_view(), name="spend"),
    path("subscriptions", SubscriptionView.as_view(), name="subscriptions"),
]
