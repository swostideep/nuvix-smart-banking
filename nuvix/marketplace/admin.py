from django.contrib import admin

from nuvix.marketplace.models import Offer, Partner, Product


@admin.register(Partner)
class PartnerAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "payout_per_funded")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "partner",
        "product_type",
        "apr_min",
        "apr_max",
        "min_credit_score",
        "is_active",
    )
    list_filter = ("product_type", "is_active", "partner")
    search_fields = ("name", "partner__name")


@admin.register(Offer)
class OfferAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "product",
        "estimated_apr",
        "approval_odds",
        "estimated_benefit",
        "rank_score",
        "clicked_at",
    )
    raw_id_fields = ("user", "product")
