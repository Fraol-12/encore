from rest_framework import serializers
from .models import Playlist


class PlaylistSerializer(serializers.ModelSerializer):
    class Meta:
        model = Playlist
        fields = [
            'id', 'title', 'description', 'youtube_playlist_id',
            'spotify_playlist_id', 'sync_status', 'source_status',
            'last_synced_at', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sync_status', 'source_status', 'last_synced_at', 'created_at', 'updated_at']