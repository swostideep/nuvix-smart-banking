"""Every user has exactly one profile, guaranteed at creation time."""

from __future__ import annotations

from django.db.models.signals import post_save
from django.dispatch import receiver

from nuvix.accounts.models import Profile, User


@receiver(post_save, sender=User)
def create_profile(sender, instance: User, created: bool, **kwargs) -> None:
    if created:
        Profile.objects.get_or_create(user=instance)
