from django.urls import include, path
from rest_framework.routers import DefaultRouter

from nuvix.banking.views import (
    CashflowView,
    DetectRecurringView,
    InstitutionListView,
    LinkedAccountViewSet,
    RecurringSeriesView,
    TransactionViewSet,
)

app_name = "banking"

# trailing_slash=False so router routes match the hand-written paths in
# this file. A single API that answers on /debt/debts/ but /debt/plans
# forces every client to remember which is which.
router = DefaultRouter(trailing_slash=False)
router.register("accounts", LinkedAccountViewSet, basename="linked-account")
router.register("transactions", TransactionViewSet, basename="transaction")

urlpatterns = [
    path("institutions", InstitutionListView.as_view(), name="institutions"),
    path("recurring", RecurringSeriesView.as_view(), name="recurring"),
    path("recurring/detect", DetectRecurringView.as_view(), name="recurring-detect"),
    path("cashflow", CashflowView.as_view(), name="cashflow"),
    path("", include(router.urls)),
]
