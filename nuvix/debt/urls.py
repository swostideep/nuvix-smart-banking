from django.urls import include, path
from rest_framework.routers import DefaultRouter

from nuvix.debt.views import (
    ActivePlanView,
    BudgetForTargetView,
    CompareStrategiesView,
    CreatePlanView,
    DebtViewSet,
    PaymentRecordViewSet,
    PlanListView,
    SimulatePlanView,
)

app_name = "debt"

# trailing_slash=False so router routes match the hand-written paths in
# this file. A single API that answers on /debt/debts/ but /debt/plans
# forces every client to remember which is which.
router = DefaultRouter(trailing_slash=False)
router.register("debts", DebtViewSet, basename="debt")
router.register("payments", PaymentRecordViewSet, basename="payment")

urlpatterns = [
    path("plans/simulate", SimulatePlanView.as_view(), name="plan-simulate"),
    path("plans/compare", CompareStrategiesView.as_view(), name="plan-compare"),
    path("plans/budget-for-target", BudgetForTargetView.as_view(), name="plan-budget-target"),
    path("plans/active", ActivePlanView.as_view(), name="plan-active"),
    path("plans", PlanListView.as_view(), name="plan-list"),
    path("plans/create", CreatePlanView.as_view(), name="plan-create"),
    path("", include(router.urls)),
]
