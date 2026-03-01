from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from encrypted_model_fields.fields import EncryptedCharField 
from datetime import timedelta 
import requests 
import time
from django.conf import settings

class CustomUserManager(BaseUserManager):
    """
    Custom manager for CustomUser that enforces email as the unique identifier.
    """
    def create_user(self, email, password=None, **extra_fields):
        """
        Create and save a user with the given email and password.
        """
        if not email:
            raise ValueError(_("The Email field must be set"))
        
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        """
        Create and save a superuser with the given email and password.
        """
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError(_("Superuser must have is_staff=True."))
        if extra_fields.get("is_superuser") is not True:
            raise ValueError(_("Superuser must have is_superuser=True."))

        return self.create_user(email, password, **extra_fields)


class CustomUser(AbstractBaseUser, PermissionsMixin):
    """
    Custom user model where email is the unique identifier instead of username.
    """
    email = models.EmailField(
        _("email address"),
        unique=True,
        error_messages={
            "unique": _("A user with that email already exists."),
        },
    )
    is_active = models.BooleanField(
        _("active"),
        default=True,
        help_text=_(
            "Designates whether this user should be treated as active. "
            "Unselect this instead of deleting accounts."
        ),
    )
    is_staff = models.BooleanField(
        _("staff status"),
        default=False,
        help_text=_("Designates whether the user can log into this admin site."),
    )
    date_joined = models.DateTimeField(_("date joined"), default=timezone.now)

    objects = CustomUserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []  # No additional required fields beyond email

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        ordering = ["email"]

    def __str__(self):
        return self.email

    def get_full_name(self):
        return self.email

    def get_short_name(self):
        return self.email
    
class SpotifyAccount(models.Model):
    user = models.OneToOneField(
        'CustomUser',
        on_delete=models.CASCADE,
        related_name='spotify_account'
    )

    spotify_user_id = models.CharField(max_length=100, blank=True)  # from /me
    access_token = EncryptedCharField(max_length=500)
    refresh_token = EncryptedCharField(max_length=500, blank=True, null=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    scope = models.TextField(blank=True)  # space-separated scopes
    is_active = models.BooleanField(default=True)  # false if revoked

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Spotify for {self.user.email}"

    class Meta:
        verbose_name = "Spotify Account"
        verbose_name_plural = "Spotify Accounts"  

    def is_expired(self, buffer_minutes=5):
        """Check if access token is expired (with buffer to avoid edge race)."""
        if not self.expires_at:
            return True
        return timezone.now() >= (self.expires_at - timedelta(minutes=buffer_minutes))

    def refresh(self):
        """Refresh access token using refresh_token."""
        if not self.refresh_token:
            raise ValueError("No refresh token — user must re-authenticate")

        payload = {
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token,
            'client_id': settings.SPOTIFY_CLIENT_ID,
            'client_secret': settings.SPOTIFY_CLIENT_SECRET,
        }

        try:
            data = None
            last_error = None
            for attempt in range(1, 4):
                resp = requests.post(settings.SPOTIFY_TOKEN_URL, data=payload, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    break
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                    retry_after = resp.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else float(2 ** (attempt - 1))
                    time.sleep(min(delay, 30.0))
                    continue
                last_error = RuntimeError(f"Spotify refresh failed ({resp.status_code}): {resp.text[:300]}")
                break

            if data is None:
                raise last_error or RuntimeError("Spotify refresh failed with unknown error")

            self.access_token = data['access_token']
            self.expires_at = timezone.now() + timedelta(seconds=data['expires_in'])

            # Spotify sometimes rotates refresh_token — update if provided
            if 'refresh_token' in data:
                self.refresh_token = data['refresh_token']

            self.scope = data.get('scope', self.scope)
            self.save(update_fields=['access_token', 'refresh_token', 'expires_at', 'scope'])

            print(f"Refreshed token for user {self.user.email} — new expiry: {self.expires_at}")

        except (requests.RequestException, RuntimeError) as e:
            print(f"Refresh failed for {self.user.email}: {str(e)}")
            self.is_active = False
            self.save(update_fields=['is_active'])
            raise RuntimeError(f"Spotify token refresh failed: {str(e)}")
