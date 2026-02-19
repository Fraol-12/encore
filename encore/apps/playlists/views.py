from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import status

from .models import Playlist, PlaylistItem, SyncOperation
from .serializers import PlaylistSerializer, PlaylistItemSerializer
from apps.users.permissions import IsOwner



class PlaylistViewSet(viewsets.ModelViewSet):
    queryset = Playlist.objects.all()
    serializer_class = PlaylistSerializer

    permission_classes = [IsAuthenticated, IsOwner]

    def get_queryset(self):
        """
        Only return playlists owned by the authenticated user. 
        """
        return self.queryset.filter(user=self.request.user)

    def perform_create(self, serializer):
        """
        Automatically assign the authenticated user as owner on create.
        """
        serializer.save(user=self.request.user)

    @action(detail=True, methods=['get'], url_path='items')
    def items(self, request, pk=None):
        playlist = self.get_object()    
        items = playlist.items.all().order_by('position') 
        serializer = PlaylistItemSerializer(items, many=True) 
        return Response(serializer.data)  
    @action(detail=True, methods=['post'], url_path='sync')
    def sync(self, request, pk=None):
        playlist = self.get_object() 

        operation = SyncOperation.objects.create(
            playlist=playlist,
            status='queued',
            triggered_by='user'
        )

        playlist.sync_status = 'queued'
        playlist.save(update_fields=['sync_status'])

        return Response({
            'status':'queued',
            'sync_operation_id': operation.id,
            'message':'Sync queued. poll /api/sync-operations/{id}/for status.'
        }, status=202)
        