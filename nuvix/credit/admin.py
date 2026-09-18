from django.contrib import admin

from nuvix.credit.models import CreditProfile, RiskAssessment, ScoreSnapshot


@admin.register(CreditProfile)
class CreditProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "score",
        "band",
        "total_balance",
        "total_credit_limit",
        "last_scored_at",
    )
    list_filter = ("band",)
    raw_id_fields = ("user",)


@admin.register(ScoreSnapshot)
class ScoreSnapshotAdmin(admin.ModelAdmin):
    list_display = ("user", "captured_on", "score", "delta")
    date_hierarchy = "captured_on"
    raw_id_fields = ("user",)


@admin.register(RiskAssessment)
class RiskAssessmentAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "decision",
        "risk_grade",
        "probability_of_default",
        "model_version",
        "created_at",
    )
    list_filter = ("decision", "risk_grade", "model_version")
    raw_id_fields = ("user",)
