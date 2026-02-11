from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator

class Playlist(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='Playlists'
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)

    youtube_playlist_id = models.CharField(

        max_length=100,
        blank=True,
        null=True,
        help_text="YouTube Playlist ID (e.g. PL...) - unique per user"
    )
    spotify_playlist_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Spotify Playlist ID once created"
    )
    spotify_playlist_uri = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="spotify:Playlist:... URI"
    )
    sync_status = models.CharField(
        max_length=20,
        default='idle',
        choices=[
            ('idle', 'Idle'),
            ('queued', 'Queued'),
            ('syncing', 'Syncing'),
            ('partial', 'Partial Success'),
            ('failed', 'Failed'),
            ('success', 'Success'),
        ]
    )
    source_status = models.CharField(
        max_length=20,
        default='active',
        choices=[
            ('active', 'Active'),
            ('unavailable', 'Unavailable'),
            ('deleted', 'Deleted'),
            ('private', 'Private'),
        ]
    )
    last_synced_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'youtube_playlist_id']),
            models.Index(fields=['user', 'spotify_playlist_id']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'youtube_playlist_id'],
                condition=models.Q(youtube_playlist_id__isnull=False),
                name='unique_youtube_per_user'
            ),
        ]

    def __str__(self):
        return f"{self.title} ({self.user.email})"
    
class PlaylistItem(models.Model):
    playlist = models.ForeignKey(
        Playlist,
        on_delete=models.CASCADE,
        related_name='items'
    )    
    youtube_video_id = models.CharField(max_length=100)
    title = models.CharField(max_length=500)
    channel_title = models.CharField(max_length=255, blank=True)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    thumbnail_url = models.URLField(max_length=500, blank=True, null=True)
    position = models.PositiveIntegerField(
        validators=[MinValueValidator(0)],
        help_text="Original position in YouTube Playlist"
    )
    is_removed_from_source = models.BooleanField(
        default=False,
        help_text="True if video 404s or is private on YouTube"
    )

    added_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['position']
        unique_together = ('playlist', 'youtube_video_id')

        indexes = [
            models.Index(fields=['playlist', 'youtube_video_id']),
            models.Index(fields=['playlist', 'position']),
        ]

    def __str__(self):
        return f"{self.title} in {self.playlist.title}"    