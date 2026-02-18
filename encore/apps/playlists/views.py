from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from .models import Playlist
from .serializers import PlaylistSerializer  
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