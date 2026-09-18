"""Abstract model plumbing reused by every domain app."""

from __future__ import annotations

import uuid

from django.db import models


class TimeStampedModel(models.Model):
    """UUID primary key plus audit timestamps.

    UUIDs rather than sequential integers because record identifiers are
    exposed to mobile clients; a sequential id leaks how many users exist and
    invites enumeration of other people's financial records.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ("-created_at",)


class SoftDeleteQuerySet(models.QuerySet):
    def alive(self) -> SoftDeleteQuerySet:
        return self.filter(deleted_at__isnull=True)


class SoftDeleteModel(TimeStampedModel):
    """Financial records are archived, never destroyed.

    Deleting a debt or a linked account must not invalidate the payoff plans
    and statements that already referenced it, so deletion is a timestamp.
    """

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = SoftDeleteQuerySet.as_manager()

    class Meta(TimeStampedModel.Meta):
        abstract = True

    def soft_delete(self) -> None:
        from django.utils import timezone

        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at", "updated_at"])
