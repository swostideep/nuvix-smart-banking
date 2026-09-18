from django.contrib import admin

from nuvix.analytics.models import (
    FunnelEvent,
    MarketingSpend,
    MarketplaceRevenue,
    Subscription,
    TouchPoint,
)


@admin.register(TouchPoint)
class TouchPointAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "channel", "campaign", "user", "anonymous_id")
    list_filter = ("channel",)
    date_hierarchy = "occurred_at"
    raw_id_fields = ("user",)


@admin.register(MarketingSpend)
class MarketingSpendAdmin(admin.ModelAdmin):
    list_display = ("spend_on", "channel", "campaign", "amount", "clicks", "impressions")
    list_filter = ("channel",)
    date_hierarchy = "spend_on"


@admin.register(FunnelEvent)
class FunnelEventAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "step", "user", "anonymous_id")
    list_filter = ("step",)
    date_hierarchy = "occurred_at"
    raw_id_fields = ("user",)


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "plan", "monthly_price", "started_on", "cancelled_on")
    list_filter = ("plan",)
    raw_id_fields = ("user",)


@admin.register(MarketplaceRevenue)
class MarketplaceRevenueAdmin(admin.ModelAdmin):
    list_display = ("user", "amount", "earned_on")
    date_hierarchy = "earned_on"
    raw_id_fields = ("user", "offer")
