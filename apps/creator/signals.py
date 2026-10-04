"""Creator signals (Phase 12).

Auto-create CreatorProfile when a creator application is approved.
Ensure one-to-one consistency between User and CreatorProfile.

 Signals are deliberately minimal - business rules live in the service layer.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver

from accounts.models import User
from .models import CreatorProfile, CreatorApplication
from .services import approve_application


@receiver(post_save, sender=CreatorApplication)
def on_creator_application_save(sender, instance, created, **kwargs):
    """Handle creator application status changes.

    When a application is approved, create the CreatorProfile
    and link it to the applicant's User.
    """
    if not created:
        return

    # Only handle new applications; status changes are handled
    # by the admin/service layer directly
    if instance.status == instance.Status.APPROVED:
        # Create the profile if it doesn't exist
        profile, _ = CreatorProfile.objects.get_or_create(
            user=instance.applicant,
            defaults={
                "display_name": instance.requested_display_name or instance.applicant.get_full_name() or instance.applicant.email,
                "slug": instance.applicant.username or instance.applicant.email.split("@")[0],
                "status": "approved",  # Will be converted to CreatorStatus.APPROVED
            },
        )
        # Note: we set status via the model's own choices; this is a simplified approach
        # The full approve_application service function should be used for proper state management