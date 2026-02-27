from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from django.conf import settings
#from django.core.exceptions import ValidationError
from rest_framework.exceptions import ValidationError
from django.db import transaction, IntegrityError
from django.utils import timezone
from ..models import Playlist, PlaylistItem, SyncOperation


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
            print(f"YouTube API error status: {e.resp.status}")
            print(f"YouTube error content: {e.content.decode('utf-8')}")
            if e.resp.status in (403, 429):
                raise ValidationError(f"YouTube API quota/access error: {e.content.decode('utf-8')}")
            # ... rest unchanged
            if e.resp.status == 404:
                raise ValidationError(f"Playlist '{playlist_id}' does not exist.")
            raise ValidationError(f"YouTube API error: {str(e)}")

    @transaction.atomic
    def import_playlist_items(self, playlist: Playlist):
        """
        Fetch ALL items from the YouTube playlist (handles pagination).
        Stores as PlaylistItem objects.
        Uses bulk_create for performance.
        Transactional — all or nothing.
        """
        if not playlist.youtube_playlist_id:
            return 0  # nothing to do

        items_to_create = []
        next_page_token = None
        total_fetched = 0

        while True:
            try:
                response = self.client.playlistItems().list(
                    part='snippet,contentDetails',
                    playlistId=playlist.youtube_playlist_id,
                    maxResults=50,               # max allowed per page
                    pageToken=next_page_token
                ).execute()

                for yt_item in response.get('items', []):
                    snippet = yt_item['snippet']
                    content = yt_item['contentDetails']

                    # Skip deleted/unavailable videos (YouTube sometimes returns them)
                    if content.get('videoId') is None:
                        continue

                    items_to_create.append(PlaylistItem(
                        playlist=playlist,
                        youtube_video_id=content['videoId'],
                        title=snippet['title'],
                        channel_title=snippet['channelTitle'],
                        position=snippet['position'],
                        thumbnail_url=snippet.get('thumbnails', {}).get('high', {}).get('url'),
                        duration_seconds=None,  # we'll fetch later or leave null for now
                        added_at=timezone.now(),
                    ))
                    total_fetched += 1

                next_page_token = response.get('nextPageToken')
                if not next_page_token:
                    break

            except HttpError as e:
                if e.resp.status in (403, 429):
                    raise ValidationError("YouTube API quota exceeded or rate limited. Try again later.")
                raise ValidationError(f"Failed to fetch playlist items: {str(e)}")

        if items_to_create:
            # Idempotent: skip already existing video IDs in this playlist
            existing_ids = set(
                PlaylistItem.objects.filter(playlist=playlist)
                .values_list('youtube_video_id', flat=True)
            )

            new_items = [
                item for item in items_to_create
                if item.youtube_video_id not in existing_ids
            ]

            PlaylistItem.objects.bulk_create(new_items, ignore_conflicts=True)

        # Update count and timestamp
        playlist.youtube_item_count = PlaylistItem.objects.filter(playlist=playlist).count()
        playlist.youtube_last_fetched_at = timezone.now()
        playlist.save(update_fields=['youtube_item_count', 'youtube_last_fetched_at'])

        return total_fetched

    def create_from_youtube(self, user, youtube_playlist_id: str) -> Playlist:
        """
        High-level method: validate, create Playlist, import items.
        All in one transaction.
        """
        metadata = self.get_playlist_metadata(youtube_playlist_id)

        with transaction.atomic():

            try:
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
                    sync_status='success',
                )
            except IntegrityError:
                raise ValidationError(
                    "You have already imported this YouTube playlist."
                )

            self.import_playlist_items(playlist)

        return playlist
        
    def _parse_iso_duration(self, iso_str: str) -> int:
        """Convert ISO 8601 duration (PTnHnMnS) to seconds."""
        import re
        pattern = re.compile(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?')
        match = pattern.match(iso_str)
        if not match:
            return None
        hours, minutes, seconds = match.groups()
        total = 0
        if hours: total += int(hours) * 3600
        if minutes: total += int(minutes) * 60
        if seconds: total += int(seconds)
        return total
        
    @transaction.atomic
    def resync_playlist(self, playlist: Playlist) -> dict:
        """
        Re-sync: fetch current YouTube state, append missing items, refresh metadata.
        Fully atomic — rollback on any failure.
        Includes batched duration fetching.
        """
        if not playlist.youtube_playlist_id:
            return {'status': 'no_source', 'added': 0, 'errors': []}

        operation = SyncOperation.objects.create(
            playlist=playlist,
            status='running',
            triggered_by='user'  # or 'cron' later
        )

        errors = []
        added_count = 0

        try:
            # 1. Fetch current metadata (title may have changed, etc.)
            metadata = self.get_playlist_metadata(playlist.youtube_playlist_id)

            # 2. Fetch all current video IDs + basic item data
            current_items = PlaylistItem.objects.filter(playlist=playlist)
            existing_video_ids = set(current_items.values_list('youtube_video_id', flat=True))

            new_items = []
            all_video_ids = []  # for duration batch
            fetched_ids = set()
            next_page_token = None

            while True:
                resp = self.client.playlistItems().list(
                    part='snippet,contentDetails',
                    playlistId=playlist.youtube_playlist_id,
                    maxResults=50,
                    pageToken=next_page_token
                ).execute()

                for yt_item in resp.get('items', []):
                    video_id = yt_item['contentDetails']['videoId']
                    fetched_ids.add(video_id)
                    all_video_ids.append(video_id)

                    if video_id in existing_video_ids:
                        continue

                    snippet = yt_item['snippet']
                    new_items.append(PlaylistItem(
                        playlist=playlist,
                        youtube_video_id=video_id,
                        title=snippet['title'],
                        channel_title=snippet['channelTitle'],
                        position=snippet['position'],
                        thumbnail_url=snippet['thumbnails'].get('high', {}).get('url'),
                        duration_seconds=None,
                        added_at=timezone.now(),
                    ))
                    added_count += 1

                next_page_token = resp.get('nextPageToken')
                if not next_page_token:
                    break

            # 3. Batch fetch durations for ALL videos (new + existing)
            if all_video_ids:
                for i in range(0, len(all_video_ids), 50):
                    batch = all_video_ids[i:i+50]
                    try:
                        vid_resp = self.client.videos().list(
                            part='contentDetails',
                            id=','.join(batch)
                        ).execute()

                        for vid in vid_resp.get('items', []):
                            vid_id = vid['id']
                            duration_iso = vid['contentDetails'].get('duration')
                            if duration_iso:
                                duration_sec = self._parse_iso_duration(duration_iso)
                                # Update existing items
                                PlaylistItem.objects.filter(
                                    playlist=playlist,
                                    youtube_video_id=vid_id
                                ).update(duration_seconds=duration_sec)

                    except HttpError as e:
                        errors.append(f"Duration batch failed: {str(e)}")

            # 4. Save new items
            if new_items:
                PlaylistItem.objects.bulk_create(new_items, ignore_conflicts=True)

            # 5. Refresh playlist metadata
            playlist.title = metadata['title']
            playlist.description = metadata['description']
            playlist.youtube_channel_id = metadata['youtube_channel_id']
            playlist.youtube_channel_title = metadata['youtube_channel_title']
            playlist.youtube_item_count = len(fetched_ids)
            playlist.youtube_thumbnail_url = metadata['youtube_thumbnail_url']
            playlist.youtube_published_at = metadata['youtube_published_at']
            playlist.youtube_privacy_status = metadata['youtube_privacy_status']
            playlist.youtube_last_fetched_at = timezone.now()
            playlist.sync_status = 'success' if not errors else 'partial'
            playlist.save()

            # 6. Finalize operation
            operation.status = 'completed' if not errors else 'partial'
            operation.matched_count = len(fetched_ids)   # total items now
            operation.unmatched_count = len(errors)      # abuse field for error count
            operation.errors = {'errors': errors} if errors else None
            operation.ended_at = timezone.now()
            operation.save()

            return {
                'status': operation.status,
                'added': added_count,
                'total_now': len(fetched_ids),
                'errors': errors
            }

        except Exception as e:
            operation.status = 'failed'
            operation.errors = {'error': str(e)}
            operation.ended_at = timezone.now()
            operation.save()
            raise

