from rest_framework import viewsets, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework.response import Response
from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework.generics import RetrieveAPIView
from django.db import transaction


from .models import Playlist, SyncOperation
from .serializers import PlaylistSerializer, PlaylistItemSerializer, SyncOperationSerializer
from apps.users.permissions import IsOwner
from .services.youtube_service import YouTubeService 


class SyncOperationDetailView(RetrieveAPIView):
    """
    Allows the playlist owner to check the status of a sync operation.
    """
    queryset = SyncOperation.objects.all()
    serializer_class = SyncOperationSerializer
    permission_classes = [IsAuthenticated, IsOwner]
    lookup_field = 'pk'  # default, but explicit is good

    def get_queryset(self):
        """
        Only allow access to sync operations belonging to the current user's playlists.
        """
        return SyncOperation.objects.filter(playlist__user=self.request.user)

class PlaylistViewSet(viewsets.ModelViewSet):
    queryset = Playlist.objects.all()
    serializer_class = PlaylistSerializer

    permission_classes = [IsAuthenticated, IsOwner]

    def get_queryset(self):
        """
        Only return playlists owned by the authenticated user. 
        """
        return self.queryset.filter(user=self.request.user)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        youtube_id = serializer.validated_data.get('youtube_playlist_id')
        if not youtube_id:
            return Response(
                {"youtube_playlist_id": ["This field is required."]},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            service = YouTubeService()
            playlist = service.create_from_youtube(
                user=request.user,
                youtube_playlist_id=youtube_id
            )
            # No need to call perform_create — service already sets user
            return Response(self.get_serializer(playlist).data, status=status.HTTP_201_CREATED)

        except ValidationError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='sync')
    def sync(self, request, pk=None):
        playlist = self.get_object()

        # Idempotency / concurrency check
        active_sync = SyncOperation.objects.filter(
            playlist=playlist,
            status__in=['queued', 'running']
        ).exists()

        if active_sync:
            return Response(
                {"detail": "A sync is already queued or running for this playlist."},
                status=status.HTTP_409_CONFLICT
            )

        # Create new operation
        operation = SyncOperation.objects.create(
            playlist=playlist,
            status='running',
            triggered_by='user',
            started_at=timezone.now()
        )

        try:
            with transaction.atomic():
                # Clean slate: delete unmatched/unconfirmed items (optional - adjust policy)
                # PlaylistItem.objects.filter(playlist=playlist, trackmatch__isnull=True).delete()

                # Real work placeholder (replace with YouTube fetch later)
                # For now: simulate work
                # service = YouTubeService()
                # service.import_playlist_items(playlist)

                # Mark success (stub)
                operation.status = 'completed'
                operation.ended_at = timezone.now()
                operation.save()

                playlist.sync_status = 'completed'
                playlist.save(update_fields=['sync_status'])

            return Response({
                'status': operation.status,
                'sync_operation_id': operation.id,
                'message': 'Sync completed successfully.'
            }, status=status.HTTP_200_OK)

        except Exception as e:
            # Rollback is automatic via transaction
            operation.status = 'failed'
            operation.errors = {"error": str(e)}
            operation.ended_at = timezone.now()
            operation.save()

            playlist.sync_status = 'failed'
            playlist.save(update_fields=['sync_status'])

            return Response({
                'status': 'failed',
                'sync_operation_id': operation.id,
                'message': f'Sync failed: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
