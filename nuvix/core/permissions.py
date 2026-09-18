"""Object-level permissions.

The single most important invariant in a consumer-finance backend: a user may
only ever read or write their own financial records.
"""

from __future__ import annotations

from rest_framework import permissions
from rest_framework.request import Request
from rest_framework.views import APIView


class IsOwner(permissions.BasePermission):
    """Authenticated, and the object belongs to the requesting user.

    ``has_permission`` is implemented as well as ``has_object_permission``,
    and that is not incidental. Setting ``permission_classes = [IsOwner]``
    *replaces* the project default of ``IsAuthenticated`` rather than adding to
    it, so a class that only defined object-level checks would leave every
    collection route open to anonymous callers -- the list view never reaches
    an object to check. The failure is silent: the queryset filters on
    ``AnonymousUser`` and the view returns a confusing 400 instead of a 401,
    which reads like a validation bug rather than a missing auth check.

    Views are still expected to scope their queryset by user; this is the
    second line of defence, not the first.
    """

    message = "You do not have access to this record."

    def has_permission(self, request: Request, view: APIView) -> bool:
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request: Request, view: APIView, obj: object) -> bool:
        owner = getattr(obj, "user", None)
        if owner is None:
            owner = obj if hasattr(obj, "is_authenticated") else None
        return owner == request.user


class IsStaffOrReadOnly(permissions.BasePermission):
    """Partner catalogue is world-readable to authenticated users, staff-writable."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        if request.method in permissions.SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        return bool(request.user and request.user.is_staff)
