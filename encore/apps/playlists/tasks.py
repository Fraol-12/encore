import logging
import math

from celery import shared_task
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone

from apps.playlists.models import Playlist, SyncOperation, TrackMatch
from apps.playlists.services.retry_utils import ForbiddenAPIError, UnauthorizedAPIError
from apps.playlists.services.spotify_matching import (
    SpotifyMatchingService,
    SpotifyRateLimited,
    SpotifySearchUnavailable,
)
from apps.playlists.services.spotify_service import SpotifyService
from apps.playlists.services.youtube_service import YouTubeService

logger = logging.getLogger(__name__)


def _map_playlist_status(operation_status: str) -> str:
    if operation_status == "completed":
        return "success"
    if operation_status == "running":
        return "syncing"
    return operation_status


def _unique_uris(uris: list[str]) -> list[str]:
    seen = set()
    ordered = []
    for uri in uris:
        if uri and uri not in seen:
            seen.add(uri)
            ordered.append(uri)
    return ordered


def _missing_required_scopes(granted_scope: str) -> list[str]:
    required = {scope for scope in settings.SPOTIFY_SCOPES.split() if scope}
    granted = {scope for scope in (granted_scope or "").split() if scope}
    return sorted(required - granted)


class SyncCancelledError(RuntimeError):
    """Raised when an operation is marked cancelled while task is running."""


def _is_cancelled(operation_id: int) -> bool:
    operation = SyncOperation.objects.filter(id=operation_id).values("status", "errors").first()
    if not operation:
        return True
    errors = operation.get("errors")
    return operation.get("status") == "failed" and isinstance(errors, dict) and errors.get("cancelled") is True


@shared_task
def process_sync(sync_operation_id: int):
    logger.info("Starting process_sync for operation=%s", sync_operation_id)

    try:
        operation = SyncOperation.objects.select_related("playlist", "playlist__user").get(id=sync_operation_id)
    except SyncOperation.DoesNotExist:
        logger.error("Sync operation %s not found", sync_operation_id)
        return

    playlist: Playlist = operation.playlist

    try:
        account = playlist.user.spotify_account
    except ObjectDoesNotExist:
        operation.status = "failed"
        operation.errors = {"error": "No Spotify account linked"}
        operation.error_count = 1
        operation.ended_at = timezone.now()
        operation.save(update_fields=["status", "errors", "error_count", "ended_at"])

        playlist.sync_status = "failed"
        playlist.save(update_fields=["sync_status", "updated_at"])
        return

    if not account.is_active:
        operation.status = "failed"
        operation.errors = {"error": "Spotify account is inactive. Re-link required."}
        operation.error_count = 1
        operation.ended_at = timezone.now()
        operation.save(update_fields=["status", "errors", "error_count", "ended_at"])

        playlist.sync_status = "failed"
        playlist.save(update_fields=["sync_status", "updated_at"])
        return

    missing_scopes = _missing_required_scopes(account.scope)
    if missing_scopes:
        operation.status = "failed"
        operation.errors = {
            "error": "Spotify account missing required scopes",
            "missing_scopes": missing_scopes,
            "action": "Re-link via /api/spotify/login/ and approve all requested permissions.",
        }
        operation.error_count = 1
        operation.ended_at = timezone.now()
        operation.save(update_fields=["status", "errors", "error_count", "ended_at"])

        playlist.sync_status = "failed"
        playlist.save(update_fields=["sync_status", "updated_at"])
        return

    operation.status = "running"
    operation.started_at = timezone.now()
    operation.save(update_fields=["status", "started_at"])

    playlist.sync_status = "syncing"
    playlist.save(update_fields=["sync_status", "updated_at"])

    errors: list[dict] = []
    unmatched = 0
    matched_items = 0
    added_tracks = 0
    removed_tracks = 0
    search_unavailable_count = 0
    rate_limit_retry_after_seconds = 0.0

    try:
        if account.is_expired():
            account.refresh()

        youtube_service = YouTubeService()
        source_summary = youtube_service.resync_playlist(playlist)

        matcher = SpotifyMatchingService(account.access_token)
        spotify = SpotifyService(account.access_token)
        spotify_profile = spotify.get_current_user()
        current_spotify_user_id = spotify_profile.get("id")
        if current_spotify_user_id and current_spotify_user_id != account.spotify_user_id:
            account.spotify_user_id = current_spotify_user_id
            account.save(update_fields=["spotify_user_id", "updated_at"])

        desired_uris: list[str] = []
        playlist_items = playlist.items.filter(is_removed_from_source=False).order_by("position")[:500]

        for index, item in enumerate(playlist_items, start=1):
            if index % 10 == 0 and _is_cancelled(operation.id):
                raise SyncCancelledError(f"Sync operation {operation.id} cancelled by user")
            try:
                matched_uri = matcher.match_item(item)
                if matched_uri:
                    matched_items += 1
                    desired_uris.append(matched_uri)
                else:
                    unmatched += 1
            except SpotifySearchUnavailable as exc:
                unmatched += 1
                search_unavailable_count += 1
                errors.append(
                    {
                        "item_id": item.id,
                        "error": str(exc),
                        "type": "spotify_search_unavailable",
                    }
                )
                if isinstance(exc, SpotifyRateLimited):
                    retry_after = exc.retry_after_seconds or 0.0
                    if retry_after > rate_limit_retry_after_seconds:
                        rate_limit_retry_after_seconds = retry_after
                    logger.warning(
                        "Aborting sync operation=%s due to Spotify rate limit at playlist item=%s",
                        operation.id,
                        item.id,
                    )
                    break
            except UnauthorizedAPIError:
                raise
            except Exception as exc:  # noqa: BLE001
                unmatched += 1
                errors.append({"item_id": item.id, "error": str(exc)})

        desired_uris = _unique_uris(desired_uris)
        source_item_count = len(playlist_items)

        if source_item_count > 0 and not desired_uris:
            operation_status = "failed" if search_unavailable_count > 0 else "partial"
            summary_error = {
                "item_errors": errors,
                "summary": {
                    "source": source_summary,
                    "source_item_count": source_item_count,
                    "matched_items": matched_items,
                    "desired_track_count": 0,
                    "added_to_spotify": 0,
                    "removed_from_spotify": 0,
                    "sync_mode": playlist.sync_mode,
                    "spotify_update_skipped": "no_matched_tracks",
                    "spotify_search_unavailable_count": search_unavailable_count,
                    "rate_limited": rate_limit_retry_after_seconds > 0,
                    "retry_after_seconds": int(math.ceil(rate_limit_retry_after_seconds)) if rate_limit_retry_after_seconds > 0 else 0,
                },
            }
            with transaction.atomic():
                operation.status = operation_status
                operation.matched_count = matched_items
                operation.unmatched_count = unmatched
                operation.error_count = len(errors)
                operation.errors = summary_error
                operation.ended_at = timezone.now()
                operation.save(
                    update_fields=[
                        "status",
                        "matched_count",
                        "unmatched_count",
                        "error_count",
                        "errors",
                        "ended_at",
                    ]
                )

                playlist.sync_status = _map_playlist_status(operation.status)
                playlist.last_synced_at = timezone.now()
                playlist.save(update_fields=["sync_status", "last_synced_at", "updated_at"])

            logger.warning(
                "Sync operation=%s skipped Spotify playlist update because zero tracks matched (source_items=%s)",
                operation.id,
                source_item_count,
            )
            return

        created_new_playlist = False

        spotify_user_id = account.spotify_user_id or current_spotify_user_id
        if not spotify_user_id:
            profile = spotify.get_current_user()
            spotify_user_id = profile.get("id")
            account.spotify_user_id = spotify_user_id or account.spotify_user_id
            account.save(update_fields=["spotify_user_id", "updated_at"])
        if not spotify_user_id:
            raise RuntimeError("Unable to resolve Spotify user id for playlist creation")

        if not playlist.spotify_playlist_id:
            created = spotify.create_playlist(
                name=playlist.title,
                description=playlist.description or "Imported by Encore",
                public=False,
            )
            playlist.spotify_playlist_id = created.get("id")
            playlist.spotify_playlist_uri = created.get("uri")
            playlist.save(update_fields=["spotify_playlist_id", "spotify_playlist_uri", "updated_at"])
            created_new_playlist = True
        else:
            try:
                spotify.update_playlist(
                    playlist_id=playlist.spotify_playlist_id,
                    name=playlist.title,
                    description=playlist.description or "Imported by Encore",
                )
            except ForbiddenAPIError:
                logger.warning(
                    "Playlist %s cannot be updated by Spotify account %s. Recreating managed playlist.",
                    playlist.spotify_playlist_id,
                    spotify_user_id,
                )
                created = spotify.create_playlist(
                    name=playlist.title,
                    description=playlist.description or "Imported by Encore",
                    public=False,
                )
                playlist.spotify_playlist_id = created.get("id")
                playlist.spotify_playlist_uri = created.get("uri")
                playlist.save(update_fields=["spotify_playlist_id", "spotify_playlist_uri", "updated_at"])
                created_new_playlist = True

        # Freshly created playlists are empty by definition.
        if created_new_playlist:
            existing_uris = []
        else:
            try:
                existing_uris = spotify.get_playlist_track_uris(playlist.spotify_playlist_id)
            except ForbiddenAPIError as exc:
                logger.warning(
                    "Unable to read Spotify playlist tracks for playlist=%s. Recreating a managed playlist. %s",
                    playlist.spotify_playlist_id,
                    exc,
                )
                created = spotify.create_playlist(
                    name=playlist.title,
                    description=playlist.description or "Imported by Encore",
                    public=False,
                )
                playlist.spotify_playlist_id = created.get("id")
                playlist.spotify_playlist_uri = created.get("uri")
                playlist.save(update_fields=["spotify_playlist_id", "spotify_playlist_uri", "updated_at"])
                created_new_playlist = True
                existing_uris = []
                errors.append(
                    {
                        "spotify_playlist_read_warning": str(exc),
                        "fallback": "recreated_managed_playlist",
                    }
                )

        existing_set = set(existing_uris)
        desired_set = set(desired_uris)

        add_uris = [uri for uri in desired_uris if uri not in existing_set]
        remove_uris: list[str] = []

        if created_new_playlist:
            remove_uris = []
        elif playlist.sync_mode == "full_replace":
            remove_uris = [uri for uri in _unique_uris(existing_uris) if uri not in desired_set]
        elif playlist.sync_mode == "smart_diff":
            managed_uris = set(
                TrackMatch.objects.filter(playlist_item__playlist=playlist).values_list("spotify_track_uri", flat=True)
            )
            remove_uris = [
                uri for uri in _unique_uris(existing_uris)
                if uri in managed_uris and uri not in desired_set
            ]

        added_tracks = spotify.add_tracks(playlist.spotify_playlist_id, add_uris)
        removed_tracks = spotify.remove_tracks(playlist.spotify_playlist_id, remove_uris)

        with transaction.atomic():
            operation.status = "completed" if unmatched == 0 and not errors else "partial"
            operation.matched_count = matched_items
            operation.unmatched_count = unmatched
            operation.error_count = len(errors)
            operation.errors = {
                "item_errors": errors,
                "summary": {
                    "source": source_summary,
                    "matched_items": matched_items,
                    "desired_track_count": len(desired_uris),
                    "added_to_spotify": added_tracks,
                    "removed_from_spotify": removed_tracks,
                    "sync_mode": playlist.sync_mode,
                },
            }
            operation.ended_at = timezone.now()
            operation.save(
                update_fields=[
                    "status",
                    "matched_count",
                    "unmatched_count",
                    "error_count",
                    "errors",
                    "ended_at",
                ]
            )

            playlist.sync_status = _map_playlist_status(operation.status)
            playlist.last_synced_at = timezone.now()
            playlist.save(update_fields=["sync_status", "last_synced_at", "updated_at"])

        logger.info(
            "Completed sync operation=%s playlist=%s status=%s matched_items=%s added=%s removed=%s",
            operation.id,
            playlist.id,
            operation.status,
            matched_items,
            added_tracks,
            removed_tracks,
        )

    except SyncCancelledError:
        logger.info("Sync operation %s cancelled during execution", operation.id)
        return

    except ForbiddenAPIError as exc:
        operation.status = "failed"
        operation.error_count = operation.error_count + 1
        operation.errors = {
            "error": str(exc),
            "action": "Ensure Spotify scopes include playlist modify/read permissions and relink if needed.",
        }
        operation.ended_at = timezone.now()
        operation.save(update_fields=["status", "error_count", "errors", "ended_at"])

        playlist.sync_status = "failed"
        playlist.save(update_fields=["sync_status", "updated_at"])
        logger.exception("Spotify permission failure for sync operation=%s", operation.id)

    except UnauthorizedAPIError as exc:
        account.is_active = False
        account.save(update_fields=["is_active", "updated_at"])

        operation.status = "failed"
        operation.error_count = operation.error_count + 1
        operation.errors = {"error": str(exc), "action": "Spotify account marked inactive"}
        operation.ended_at = timezone.now()
        operation.save(update_fields=["status", "error_count", "errors", "ended_at"])

        playlist.sync_status = "failed"
        playlist.save(update_fields=["sync_status", "updated_at"])
        logger.exception("Spotify authorization failed for sync operation=%s", operation.id)

    except Exception as exc:  # noqa: BLE001
        operation.status = "failed"
        operation.error_count = operation.error_count + 1
        operation.errors = {"error": str(exc)}
        operation.ended_at = timezone.now()
        operation.save(update_fields=["status", "error_count", "errors", "ended_at"])

        playlist.sync_status = "failed"
        playlist.save(update_fields=["sync_status", "updated_at"])
        logger.exception("Fatal sync failure for operation=%s", operation.id)
