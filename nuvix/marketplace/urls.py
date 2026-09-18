from django.urls import path

from nuvix.marketplace.views import (
    MatchView,
    OfferClickView,
    OfferListView,
    ProductListView,
)

app_name = "marketplace"

urlpatterns = [
    path("products", ProductListView.as_view(), name="products"),
    path("match", MatchView.as_view(), name="match"),
    path("offers", OfferListView.as_view(), name="offers"),
    path("offers/<uuid:pk>/click", OfferClickView.as_view(), name="offer-click"),
]
