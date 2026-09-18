from django.contrib import admin

from nuvix.comms.models import CommunicationRule, Message, MessageTemplate


@admin.register(MessageTemplate)
class MessageTemplateAdmin(admin.ModelAdmin):
    list_display = ("key", "channel", "subject", "is_active")
    list_filter = ("channel", "is_active")


@admin.register(CommunicationRule)
class CommunicationRuleAdmin(admin.ModelAdmin):
    list_display = ("key", "trigger", "priority", "cooldown_days", "max_per_week", "is_active")
    list_filter = ("trigger", "is_active")


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "channel",
        "status",
        "suppression_reason",
        "scheduled_for",
        "sent_at",
    )
    list_filter = ("status", "channel", "suppression_reason")
    raw_id_fields = ("user", "rule")
    date_hierarchy = "created_at"
