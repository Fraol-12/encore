import logging
import time
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from isodate import parse_duration
from rest_framework.exceptions import ValidationError

from ..models import Playlist, PlaylistItem

logger = logging.getLogger(__name__)


class YouTubeService:
    """Encapsulates YouTube Data API v3 interactions."""

    RETRYABLE_STATUS = {429, 500, 502, 503, 504}
    MAX_ITEMS = 500

    def __init__(self):
        if not settings.YOUTUBE_API_KEY:
            raise RuntimeError("YOUTUBE_API_KEY is not configured in settings")
        self.client = build("youtube", "v3", developerKey=settings.YOUTUBE_API_KEY)

    def _parse_retry_after(self, error: HttpError) -> float | None:
        header_value = None
        if hasattr(error, "resp"):
            header_value = error.resp.get("retry-after")

        if not header_value:
            return None

        try:
            return max(0.0, float(header_value))
        except (TypeError, ValueError):
            return None

    def _execute_with_retry(self, request, operation: str):
        last_error = None
        for attempt in range(1, 4):
            try:
                return request.execute()
            except HttpError as exc:
                last_error = exc
                status = getattr(exc.resp, "status", None)
                if status in self.RETRYABLE_STATUS and attempt < 3:
                    delay = self._parse_retry_after(exc)
                    if delay is None:
                        delay = float(2 ** (attempt - 1))
                    logger.warning(
                        "YouTube transient failure during %s (%s/3), retrying in %.2fs",
                        operation,
                        attempt,
                        delay,
                    )
                    time.sleep(min(delay, 30.0))
                    continue
                raise

        raise RuntimeError(f"{operation} failed after retries") from last_error

    def _parse_iso_duration(self, iso_str: str | None) -> int | None:
        if not iso_str:
            return None
        try:
            return int(parse_duration(iso_str).total_seconds())
        except Exception:  # noqa: BLE001
            return None

    def get_video_duration(self, video_id: str) -> int | None:
        """Fetch one video's duration in seconds."""
        response = self._execute_with_retry(
            self.client.videos().list(part="contentDetails", id=video_id, maxResults=1),
            operation=f"video duration lookup {video_id}",
        )
        items = response.get("items", [])
        if not items:
            return None
        return self._parse_iso_duration(items[0].get("contentDetails", {}).get("duration"))

    def get_video_durations(self, video_ids: list[str]) -> dict[str, int | None]:
        """Fetch multiple video durations in batches of 50."""
        duration_map: dict[str, int | None] = {}
        unique_ids = list(dict.fromkeys(video_ids))
        for i in range(0, len(unique_ids), 50):
            batch = unique_ids[i : i + 50]
            response = self._execute_with_retry(
                self.client.videos().list(part="contentDetails", id=",".join(batch), maxResults=50),
                operation="video duration batch lookup",
            )
            for item in response.get("items", []):
                duration_map[item.get("id", "")] = self._parse_iso_duration(
                    item.get("contentDetails", {}).get("duration")
                )
        return duration_map

    def get_playlist_metadata(self, playlist_id: str) -> dict:
        """Validate playlist and fetch metadata."""
        try:
            response = self._execute_with_retry(
                self.client.playlists().list(part="snippet,contentDetails,status", id=playlist_id, maxResults=1),
                operation=f"playlist metadata lookup {playlist_id}",
            )

            if not response.get("items"):
                raise ValidationError(f"Playlist '{playlist_id}' not found or is private/inaccessible")

            item = response["items"][0]
            snippet = item.get("snippet", {})
            content = item.get("contentDetails", {})
            status = item.get("status", {})

            return {
                "youtube_playlist_id": playlist_id,
                "title": snippet.get("title", "Untitled Playlist"),
                "description": snippet.get("description", ""),
                "youtube_channel_id": snippet.get("channelId"),
                "youtube_channel_title": snippet.get("channelTitle", ""),
                "youtube_item_count": content.get("itemCount", 0),
                "youtube_thumbnail_url": (snippet.get("thumbnails") or {}).get("high", {}).get("url"),
                "youtube_published_at": snippet.get("publishedAt"),
                "youtube_privacy_status": status.get("privacyStatus", "public"),
            }

        except HttpError as e:
            status = getattr(e.resp, "status", None)
            if status in (403, 429):
                raise ValidationError("YouTube API quota exceeded or access denied. Try again later.")
            if status == 404:
                raise ValidationError(f"Playlist '{playlist_id}' does not exist.")
            raise ValidationError(f"YouTube API error: {str(e)}")

    @transaction.atomic
    def import_playlist_items(self, playlist: Playlist) -> int:
        """
        Fetch all items from YouTube playlist and store as PlaylistItem.
        Returns number of newly added items.
        """
        if not playlist.youtube_playlist_id:
            return 0

        fetched_items: list[dict[str, Any]] = []
        next_page_token = None

        while len(fetched_items) < self.MAX_ITEMS:
            try:
                response = self._execute_with_retry(
                    self.client.playlistItems().list(
                        part="snippet,contentDetails",
                        playlistId=playlist.youtube_playlist_id,
                        maxResults=50,
                        pageToken=next_page_token,
                    ),
                    operation=f"playlist items import {playlist.youtube_playlist_id}",
                )
            except HttpError as e:
                raise ValidationError(f"Failed to fetch playlist items: {str(e)}")

            for yt_item in response.get("items", []):
                content = yt_item.get("contentDetails", {})
                snippet = yt_item.get("snippet", {})
                video_id = content.get("videoId")
                if not video_id:
                    continue

                fetched_items.append(
                    {
                        "youtube_video_id": video_id,
                        "title": snippet.get("title", ""),
                        "channel_title": snippet.get("channelTitle", ""),
                        "position": snippet.get("position", 0),
                        "thumbnail_url": (snippet.get("thumbnails") or {}).get("high", {}).get("url"),
                    }
                )
                if len(fetched_items) >= self.MAX_ITEMS:
                    break

            next_page_token = response.get("nextPageToken")
            if not next_page_token:
                break

        existing_ids = set(
            PlaylistItem.objects.filter(playlist=playlist).values_list("youtube_video_id", flat=True)
        )
        new_payload = [row for row in fetched_items if row["youtube_video_id"] not in existing_ids]

        new_objects: list[PlaylistItem] = []
        duration_map = self.get_video_durations([row["youtube_video_id"] for row in new_payload])

        for row in new_payload:
            duration = duration_map.get(row["youtube_video_id"])
            if duration is None:
                duration = self.get_video_duration(row["youtube_video_id"])
            new_objects.append(
                PlaylistItem(
                    playlist=playlist,
                    youtube_video_id=row["youtube_video_id"],
                    title=row["title"],
                    channel_title=row["channel_title"],
                    position=row["position"],
                    thumbnail_url=row["thumbnail_url"],
                    duration_seconds=duration,
                    is_removed_from_source=False,
                )
            )

        if new_objects:
            PlaylistItem.objects.bulk_create(new_objects, ignore_conflicts=True)

        playlist.youtube_item_count = len(fetched_items)
        playlist.youtube_last_fetched_at = timezone.now()
        playlist.save(update_fields=["youtube_item_count", "youtube_last_fetched_at"])

        return len(new_objects)

    @transaction.atomic
    def create_from_youtube(self, user, youtube_playlist_id: str) -> Playlist:
        """Validate, create Playlist, and import items atomically."""
        metadata = self.get_playlist_metadata(youtube_playlist_id)

        try:
            playlist = Playlist.objects.create(
                user=user,
                youtube_playlist_id=youtube_playlist_id,
                title=metadata["title"],
                description=metadata["description"],
                youtube_channel_id=metadata["youtube_channel_id"],
                youtube_channel_title=metadata["youtube_channel_title"],
                youtube_item_count=metadata["youtube_item_count"],
                youtube_thumbnail_url=metadata["youtube_thumbnail_url"],
                youtube_published_at=metadata["youtube_published_at"],
                youtube_privacy_status=metadata["youtube_privacy_status"],
                youtube_last_fetched_at=timezone.now(),
                sync_status="success",
            )
        except IntegrityError as exc:
            raise ValidationError("You have already imported this YouTube playlist.") from exc

        self.import_playlist_items(playlist)
        return playlist

    @transaction.atomic
    def resync_playlist(self, playlist: Playlist) -> dict:
        """
        Refresh playlist metadata and items from YouTube.
        Returns summary dict used by sync operation logs.
        """
        if not playlist.youtube_playlist_id:
            return {"status": "no_source", "added": 0, "updated": 0, "removed": 0, "total": 0}

        metadata = self.get_playlist_metadata(playlist.youtube_playlist_id)

        fetched_items: list[dict[str, Any]] = []
        fetched_video_ids: list[str] = []
        next_page_token = None

        while len(fetched_items) < self.MAX_ITEMS:
            response = self._execute_with_retry(
                self.client.playlistItems().list(
                    part="snippet,contentDetails",
                    playlistId=playlist.youtube_playlist_id,
                    maxResults=50,
                    pageToken=next_page_token,
                ),
                operation=f"playlist items resync {playlist.youtube_playlist_id}",
            )

            for yt_item in response.get("items", []):
                content = yt_item.get("contentDetails", {})
                snippet = yt_item.get("snippet", {})
                video_id = content.get("videoId")
                if not video_id:
                    continue

                fetched_video_ids.append(video_id)
                fetched_items.append(
                    {
                        "youtube_video_id": video_id,
                        "title": snippet.get("title", ""),
                        "channel_title": snippet.get("channelTitle", ""),
                        "position": snippet.get("position", 0),
                        "thumbnail_url": (snippet.get("thumbnails") or {}).get("high", {}).get("url"),
                    }
                )

                if len(fetched_items) >= self.MAX_ITEMS:
                    break

            next_page_token = response.get("nextPageToken")
            if not next_page_token:
                break

        duration_map = self.get_video_durations(fetched_video_ids)

        for row in fetched_items:
            row["duration_seconds"] = duration_map.get(row["youtube_video_id"])

        existing_map = {
            item.youtube_video_id: item
            for item in PlaylistItem.objects.filter(playlist=playlist)
        }

        fetched_ids = {row["youtube_video_id"] for row in fetched_items}
        added_objects: list[PlaylistItem] = []
        update_objects: list[PlaylistItem] = []

        for row in fetched_items:
            video_id = row["youtube_video_id"]
            existing = existing_map.get(video_id)
            if existing is None:
                added_objects.append(
                    PlaylistItem(
                        playlist=playlist,
                        youtube_video_id=video_id,
                        title=row["title"],
                        channel_title=row["channel_title"],
                        position=row["position"],
                        thumbnail_url=row["thumbnail_url"],
                        duration_seconds=row["duration_seconds"],
                        is_removed_from_source=False,
                    )
                )
                continue

            changed = False
            for field in ["title", "channel_title", "position", "thumbnail_url", "duration_seconds"]:
                incoming = row[field]
                if getattr(existing, field) != incoming:
                    setattr(existing, field, incoming)
                    changed = True

            if existing.is_removed_from_source:
                existing.is_removed_from_source = False
                changed = True

            if changed:
                update_objects.append(existing)

        removed_objects: list[PlaylistItem] = []
        for video_id, item in existing_map.items():
            if video_id not in fetched_ids and not item.is_removed_from_source:
                item.is_removed_from_source = True
                removed_objects.append(item)

        if added_objects:
            PlaylistItem.objects.bulk_create(added_objects, ignore_conflicts=True)

        if update_objects:
            PlaylistItem.objects.bulk_update(
                update_objects,
                ["title", "channel_title", "position", "thumbnail_url", "duration_seconds", "is_removed_from_source", "updated_at"],
            )

        if removed_objects:
            PlaylistItem.objects.bulk_update(removed_objects, ["is_removed_from_source", "updated_at"])

        playlist.title = metadata["title"]
        playlist.description = metadata["description"]
        playlist.youtube_channel_id = metadata["youtube_channel_id"]
        playlist.youtube_channel_title = metadata["youtube_channel_title"]
        playlist.youtube_item_count = len(fetched_items)
        playlist.youtube_thumbnail_url = metadata["youtube_thumbnail_url"]
        playlist.youtube_published_at = metadata["youtube_published_at"]
        playlist.youtube_privacy_status = metadata["youtube_privacy_status"]
        playlist.youtube_last_fetched_at = timezone.now()
        playlist.source_status = "private" if metadata["youtube_privacy_status"] == "private" else "active"
        playlist.save(
            update_fields=[
                "title",
                "description",
                "youtube_channel_id",
                "youtube_channel_title",
                "youtube_item_count",
                "youtube_thumbnail_url",
                "youtube_published_at",
                "youtube_privacy_status",
                "youtube_last_fetched_at",
                "source_status",
                "updated_at",
            ]
        )

        summary = {
            "status": "completed",
            "added": len(added_objects),
            "updated": len(update_objects),
            "removed": len(removed_objects),
            "total": len(fetched_items),
        }
        logger.info("YouTube re-sync summary for playlist %s: %s", playlist.id, summary)
        return summary
