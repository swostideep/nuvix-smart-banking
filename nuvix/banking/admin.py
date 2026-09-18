from django.contrib import admin

from nuvix.banking.models import Institution, LinkedAccount, RecurringSeries, Transaction


@admin.register(Institution)
class InstitutionAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active")
    search_fields = ("name",)


@admin.register(LinkedAccount)
class LinkedAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "account_type", "current_balance", "apr", "deleted_at")
    list_filter = ("account_type", "institution")
    search_fields = ("user__email", "name")
    raw_id_fields = ("user",)


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("posted_on", "merchant", "amount", "category", "user")
    list_filter = ("category", "is_pending")
    search_fields = ("merchant", "user__email")
    raw_id_fields = ("user", "account")
    date_hierarchy = "posted_on"


@admin.register(RecurringSeries)
class RecurringSeriesAdmin(admin.ModelAdmin):
    list_display = ("label", "user", "cadence", "average_amount", "is_income", "confidence")
    list_filter = ("cadence", "is_income")
    raw_id_fields = ("user",)
