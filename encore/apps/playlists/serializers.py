from rest_framework import serializers
from .models import Playlist, PlaylistItem

Playlist
class PlaylistSerializer(serializers.ModelSerializer):
    class Meta:
        model = Playlist
        fields = [
            'id', 'title', 'description', 'youtube_playlist_id',
            'spotify_playlist_id', 'sync_status', 'source_status',
            'youtube_channel_title', 'youtube_item_count', 'youtube_thumbnail_url',
            'youtube_published_at', 'youtube_last_fetched_at', 'youtube_privacy_status',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'sync_status', 'source_status',
            'youtube_channel_title', 'youtube_item_count', 'youtube_thumbnail_url',
            'youtube_published_at', 'youtube_last_fetched_at', 'youtube_privacy_status',
            'created_at', 'updated_at'
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Make title optional for input (we fill it from YouTube)
        self.fields['title'].required = False
        self.fields['title'].allow_blank = True

class PlaylistItemSerializer(serializers.ModelSerializer):
    class Meta:        
        model = PlaylistItem 
        fields = [
            'id', 'youtube_video_id', 'title', 'channel_title',
            'duration_seconds', 'thumbnail_url', 'position',
            'is_removed_from_source', 'added_at'
        ]
        read_only_fields = fields 