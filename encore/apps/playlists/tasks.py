from celery import shared_task
from django.utils import timezone
from .models import SyncOperation, Playlist
from .services.youtube_service import YouTubeService
from apps.playlists.services.spotify_matching import SpotifyMatchingService

@shared_task
def process_sync(sync_operation_id: int):
    print(f"[CELERY] Starting process_sync for operation {sync_operation_id}")
    operation = SyncOperation.objects.get(id=sync_operation_id)
    playlist = operation.playlist
    print(f"[CELERY] Playlist {playlist.id} - {playlist.title}")

    account = playlist.user.spotify_account
    if not account:
        print("[CELERY] No Spotify account linked — aborting")
        operation.status = 'failed'
        operation.errors = {"error": "No Spotify linked"}
        operation.save()
        return

    if account.is_expired():
        print("[CELERY] Token expired — refreshing")
        account.refresh()

    operation.status = 'running'
    operation.started_at = timezone.now()
    operation.save()
    print("[CELERY] Status set to running")

    try:
        service = SpotifyMatchingService(account.access_token)
        print("[CELERY] Matching service initialized")

        matched = 0
        unmatched = 0
        errors = []

        items = playlist.items.all()
        print(f"[CELERY] Processing {items.count()} items")

        for item in items:
            print(f"[CELERY] Matching item {item.id} - {item.title}")
            try:
                uri = service.match_item(item)
                if uri:
                    matched += 1
                    print(f"[CELERY] Match found for {item.id}")
                else:
                    unmatched += 1
                    print(f"[CELERY] No match for {item.id}")
            except Exception as e:
                errors.append({'item_id': item.id, 'error': str(e)})
                unmatched += 1
                print(f"[CELERY] Error matching {item.id}: {str(e)}")

        operation.status = 'completed' if unmatched == 0 else 'partial'
        operation.matched_count = matched
        operation.unmatched_count = unmatched
        operation.error_count = len(errors)
        operation.errors = errors
        operation.ended_at = timezone.now()
        operation.save()
        print(f"[CELERY] Completed: matched={matched}, unmatched={unmatched}")

        playlist.sync_status = operation.status
        playlist.save(update_fields=['sync_status'])

    except Exception as e:
        print(f"[CELERY] Fatal error: {str(e)}")
        # ... handle ...