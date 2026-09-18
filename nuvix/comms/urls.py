from django.urls import path

from nuvix.comms.views import MessageListView, RuleListView, SweepView

app_name = "comms"

urlpatterns = [
    path("messages", MessageListView.as_view(), name="messages"),
    path("rules", RuleListView.as_view(), name="rules"),
    path("sweep", SweepView.as_view(), name="sweep"),
]
