from celery import shared_task
from django.utils import timezone
from .models import SyncOperation, Playlist
from .services.youtube_service import YouTubeService


@shared_task
def process_sync(sync_operation_id: int):
    operation = SyncOperation.objects.get(id=sync_operation_id)
    playlist = operation.playlist

    operation.status = 'running'
    operation.started_at = timezone.now()
    operation.save()

    try:
        service = YouTubeService()
        service.import_playlist_items(playlist)

        operation.status = 'completed'
        operation.ended_at = timezone.now()
        operation.save()

        playlist.sync_status = 'completed'
        playlist.save(update_fields=['sync_status'])

    except Exception as e:
        operation.status = 'failed'
        operation.errors = {"error": str(e)}
        operation.ended_at = timezone.now()
        operation.save()

        playlist.sync_status = 'failed'
        playlist.save(update_fields=['sync_status'])