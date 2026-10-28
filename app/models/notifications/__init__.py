"""Notifications model and business rules."""
from .service import NotificationService

__all__ = ["NotificationService", "IdentityMailService"]

from .account_mail import IdentityMailService
