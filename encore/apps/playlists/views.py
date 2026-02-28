from rest_framework import viewsets, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.generics import RetrieveAPIView
from django.db import transaction, IntegrityError
from .tasks import process_sync
from .models import Playlist, SyncOperation
from .serializers import PlaylistSerializer, PlaylistItemSerializer, SyncOperationSerializer
from apps.users.permissions import IsOwner
from .services.youtube_service import YouTubeService
from rest_framework.exceptions import ValidationError
from django.core.exceptions import ValidationError

class SyncOperationDetailView(RetrieveAPIView):
    queryset = SyncOperation.objects.all()
    serializer_class = SyncOperationSerializer
    permission_classes = [IsAuthenticated, IsOwner]

    def get_queryset(self):
        return SyncOperation.objects.filter(playlist__user=self.request.user)


class PlaylistViewSet(viewsets.ModelViewSet):
    queryset = Playlist.objects.all()
    serializer_class = PlaylistSerializer
    permission_classes = [IsAuthenticated, IsOwner]

    def get_queryset(self):
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
            return Response(self.get_serializer(playlist).data, status=status.HTTP_201_CREATED)
        except ValidationError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='sync')
    def sync(self, request, pk=None):
        playlist = self.get_object()

        print(f"[SYNC] User {request.user.email} triggered sync for playlist {playlist.id}")

        active = SyncOperation.objects.filter(
            playlist=playlist,
            status__in=['queued', 'running']
        )

        if active.exists():
            active_ids = list(active.values_list('id', flat=True))
            return Response(
                {"detail": f"Sync already queued/running (IDs: {active_ids})"},
                status=status.HTTP_409_CONFLICT
            )

        try:
            operation = SyncOperation.objects.create(
                playlist=playlist,
                status='queued',
                triggered_by='user'
            )
        except IntegrityError:
            return Response(
                {"detail": "A sync is already queued or running for this playlist."},
                status=status.HTTP_409_CONFLICT
            )

        process_sync.delay(operation.id)

        playlist.sync_status = 'queued'
        playlist.save(update_fields=['sync_status'])

        return Response({
            'status': 'queued',
            'sync_operation_id': operation.id,
            'message': f'Sync queued. Poll /api/sync-operations/{operation.id}/'
        }, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=['get'], url_path='items')
    def items(self, request, pk=None):
        playlist = self.get_object()
        items = playlist.items.order_by('position')
        serializer = PlaylistItemSerializer(items, many=True)
        return Response(serializer.data)