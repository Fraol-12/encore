from rest_framework import serializers
from .models import Playlist, PlaylistItem

Playlist
class PlaylistSerializer(serializers.ModelSerializer):
    class Meta:
        model = Playlist
        fields = [
            'id', 'title', 'description', 'youtube_playlist_id',
            'spotify_playlist_id', 'sync_status', 'source_status',
            'last_synced_at', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sync_status', 'source_status', 'last_synced_at', 'created_at', 'updated_at']

class PlaylistItemSerializer(serializers.ModelSerializer):
    class Meta:        
        model = PlaylistItem 
        fields = [
            'id', 'youtube_video_id', 'title', 'channel_title',
            'duration_seconds', 'thumbnail_url', 'position',
            'is_removed_from_source', 'added_at'
        ]
        read_only_fields = fields