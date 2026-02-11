from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator


class Playlist(models.Model):
    """
    Represents a logical playlist owned by a user.
    Can be linked to a YouTube source and/or a Spotify destination.
    Database is source of truth — external services are eventually consistent.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='playlists'
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)

    youtube_playlist_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="YouTube playlist ID (e.g. PL...) - unique per user"
    )
    spotify_playlist_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Spotify playlist ID once created"
    )
    spotify_playlist_uri = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="spotify:playlist:... URI"
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
    """
    An item (video) in a specific playlist.
    Enforces no duplicates within one playlist.
    Stores cached YouTube metadata to reduce API calls.
    """
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
        help_text="Original position in YouTube playlist"
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


# TrackMatch and SyncOperation will come in the next step (after we review this batch)   
    
class TrackMatch(models.Model):
    """
    Represents a potential or confirmed match between a PlaylistItem (YouTube video)
    and a Spotify track. Multiple matches per item are allowed for candidates/history.
    Only one should be marked is_active=True at a time (current best match).
    """
    playlist_item = models.ForeignKey(
        PlaylistItem,
        on_delete=models.CASCADE,
        related_name='matches'
    )
    spotify_track_id = models.CharField(max_length=50)          # e.g. '6rqhFgbbKwnb9MLmUQDhG6'
    spotify_track_uri = models.CharField(max_length=100)       # 'spotify:track:6rqhFgbbKwnb9MLmUQDhG6'

    confidence_score = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=0.0000,
        help_text="0.0000 to 1.0000 - higher = better match"
    )
    match_method = models.CharField(
        max_length=20,
        choices=[
            ('auto_fuzzy', 'Auto Fuzzy Matching'),
            ('exact_id', 'Exact YouTube ID in description/metadata'),
            ('manual', 'User Manual Override'),
        ],
        default='auto_fuzzy'
    )
    is_active = models.BooleanField(
        default=False,
        help_text="True if this is the currently selected best match"
    )
    match_metadata = models.JSONField(
        blank=True,
        null=True,
        help_text="Snapshot of Spotify API response (artist, album, duration, etc.)"
    )

    matched_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-confidence_score', '-matched_at']
        indexes = [
            models.Index(fields=['playlist_item', 'is_active']),
            models.Index(fields=['spotify_track_id']),
        ]
        constraints = [
            # Optional: prevent duplicate active matches per item
            models.UniqueConstraint(
                fields=['playlist_item', 'is_active'],
                condition=models.Q(is_active=True),
                name='unique_active_match_per_item'
            ),
        ]

    def __str__(self):
        return f"Match for {self.playlist_item} → {self.spotify_track_id} ({self.confidence_score})"


class SyncOperation(models.Model):
    """
    Records every attempt to sync a playlist (initial import or re-sync).
    Enables retry, partial failure handling, user feedback, and debugging.
    """
    playlist = models.ForeignKey(
        Playlist,
        on_delete=models.CASCADE,
        related_name='sync_operations'
    )
    status = models.CharField(
        max_length=20,
        choices=[
            ('queued', 'Queued'),
            ('running', 'Running'),
            ('completed', 'Completed'),
            ('partial', 'Partial Success'),
            ('failed', 'Failed'),
        ],
        default='queued'
    )

    matched_count = models.PositiveIntegerField(default=0)
    unmatched_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)

    errors = models.JSONField(
        blank=True,
        null=True,
        help_text="Dict of {playlist_item_id: error_reason} or general errors"
    )

    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    triggered_by = models.CharField(
        max_length=50,
        default='user',
        choices=[
            ('user', 'Manual User Trigger'),
            ('cron', 'Scheduled Re-sync'),
            ('retry', 'Retry After Failure'),
        ]
    )

    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['playlist', 'status']),
            models.Index(fields=['started_at']),
        ]

    def __str__(self):
        return f"Sync {self.status} for {self.playlist} at {self.started_at}"