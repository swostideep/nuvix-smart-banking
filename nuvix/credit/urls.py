from django.urls import path

from nuvix.credit.views import (
    AssessRiskView,
    CreditProfileView,
    RecommendedActionsView,
    RefreshFromAccountsView,
    RiskAssessmentListView,
    ScoreHistoryView,
    ScoreView,
    SimulateScoreView,
)

app_name = "credit"

urlpatterns = [
    path("profile", CreditProfileView.as_view(), name="profile"),
    path("score", ScoreView.as_view(), name="score"),
    path("score/history", ScoreHistoryView.as_view(), name="score-history"),
    path("score/refresh", RefreshFromAccountsView.as_view(), name="score-refresh"),
    path("score/simulate", SimulateScoreView.as_view(), name="score-simulate"),
    path(
        "score/recommendations", RecommendedActionsView.as_view(), name="score-recommendations"
    ),
    path("risk/assess", AssessRiskView.as_view(), name="risk-assess"),
    path("risk/assessments", RiskAssessmentListView.as_view(), name="risk-list"),
]
