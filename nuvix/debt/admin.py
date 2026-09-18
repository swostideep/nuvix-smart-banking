from django.contrib import admin

from nuvix.debt.models import Debt, PaymentRecord, PayoffPlan


@admin.register(Debt)
class DebtAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "kind", "balance", "apr", "minimum_payment", "deleted_at")
    list_filter = ("kind",)
    search_fields = ("user__email", "name")
    raw_id_fields = ("user", "linked_account")


@admin.register(PayoffPlan)
class PayoffPlanAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "strategy",
        "monthly_budget",
        "months_to_debt_free",
        "total_interest",
        "is_active",
        "created_at",
    )
    list_filter = ("strategy", "is_active")
    raw_id_fields = ("user",)


@admin.register(PaymentRecord)
class PaymentRecordAdmin(admin.ModelAdmin):
    list_display = ("debt", "user", "paid_on", "amount")
    raw_id_fields = ("user", "debt")
    date_hierarchy = "paid_on"
