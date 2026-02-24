from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from ..models import Playlist, PlaylistItem


class YouTubeService:
    """Encapsulates all YouTube Data API v3 interactions."""

    def __init__(self):
        if not settings.YOUTUBE_API_KEY:
            raise RuntimeError("YOUTUBE_API_KEY is not configured in settings")
        self.client = build('youtube', 'v3', developerKey=settings.YOUTUBE_API_KEY)

    def get_playlist_metadata(self, playlist_id: str) -> dict:
        """
        Validate playlist and fetch core metadata.
        Raises ValidationError on any failure.
        """
        try:
            response = self.client.playlists().list(
                part='snippet,contentDetails,status',
                id=playlist_id,
                maxResults=1
            ).execute()

            if not response.get('items'):
                raise ValidationError(f"Playlist '{playlist_id}' not found or is private/inaccessible")

            item = response['items'][0]

            return {
                'youtube_playlist_id': playlist_id,
                'title': item['snippet']['title'],
                'description': item['snippet'].get('description', ''),
                'youtube_channel_id': item['snippet']['channelId'],
                'youtube_channel_title': item['snippet']['channelTitle'],
                'youtube_item_count': item['contentDetails']['itemCount'],
                'youtube_thumbnail_url': item['snippet']['thumbnails'].get('high', {}).get('url'),
                'youtube_published_at': item['snippet'].get('publishedAt'),
                'youtube_privacy_status': item['status']['privacyStatus'],
            }

        except HttpError as e:
            if e.resp.status in (403, 429):
                raise ValidationError("YouTube API quota exceeded or access denied. Try again later.")
            if e.resp.status == 404:
                raise ValidationError(f"Playlist '{playlist_id}' does not exist.")
            raise ValidationError(f"YouTube API error: {str(e)}")

    @transaction.atomic
    def import_playlist_items(self, playlist: Playlist):
        """
        Fetch all items from the YouTube playlist and store them as PlaylistItem.
        Uses bulk_create for performance. Transactional — all or nothing.
        """
        if not playlist.youtube_playlist_id:
            return

        items = []
        next_page_token = None
        fetched_count = 0

        while True:
            try:
                response = self.client.playlistItems().list(
                    part='snippet,contentDetails',
                    playlistId=playlist.youtube_playlist_id,
                    maxResults=50,
                    pageToken=next_page_token
                ).execute()

                for yt_item in response.get('items', []):
                    snippet = yt_item['snippet']
                    content = yt_item['contentDetails']

                    items.append(PlaylistItem(
                        playlist=playlist,
                        youtube_video_id=content['videoId'],
                        title=snippet['title'],
                        channel_title=snippet['channelTitle'],
                        position=snippet['position'],
                        thumbnail_url=snippet['thumbnails'].get('high', {}).get('url'),
                        duration_seconds=None,  # optional: fetch later via videos.list
                        added_at=timezone.now(),
                    ))
                    fetched_count += 1

                next_page_token = response.get('nextPageToken')
                if not next_page_token:
                    break

            except HttpError as e:
                raise ValidationError(f"Failed to fetch playlist items: {str(e)}")

        if items:
            PlaylistItem.objects.bulk_create(items, ignore_conflicts=True)

            # Update playlist metadata after successful import
            playlist.youtube_item_count = fetched_count
            playlist.youtube_last_fetched_at = timezone.now()
            playlist.save(update_fields=['youtube_item_count', 'youtube_last_fetched_at'])

    def create_from_youtube(self, user, youtube_playlist_id: str) -> Playlist:
        """
        High-level method: validate, create Playlist, import items.
        All in one transaction.
        """
        metadata = self.get_playlist_metadata(youtube_playlist_id)

        with transaction.atomic():
            playlist = Playlist.objects.create(
                user=user,
                youtube_playlist_id=youtube_playlist_id,
                title=metadata['title'],
                description=metadata['description'],
                youtube_channel_id=metadata['youtube_channel_id'],
                youtube_channel_title=metadata['youtube_channel_title'],
                youtube_item_count=metadata['youtube_item_count'],
                youtube_thumbnail_url=metadata['youtube_thumbnail_url'],
                youtube_published_at=metadata['youtube_published_at'],
                youtube_privacy_status=metadata['youtube_privacy_status'],
                youtube_last_fetched_at=timezone.now(),
                sync_status='success',  # initial import succeeded
            )

            self.import_playlist_items(playlist)
            return playlist