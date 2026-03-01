from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.playlists.models import Playlist, PlaylistItem, SyncOperation, TrackMatch
from apps.playlists.services.retry_utils import ForbiddenAPIError, UnauthorizedAPIError
from apps.playlists.services.spotify_matching import (
    SpotifyMatchingService,
    SpotifyRateLimited,
)
from apps.playlists.services.youtube_service import YouTubeService
from apps.playlists.tasks import process_sync
from apps.users.models import CustomUser, SpotifyAccount


class FactoryMixin:
    def make_user(self, email="user@example.com", password="strongpass123"):
        return CustomUser.objects.create_user(email=email, password=password)

    def make_playlist(self, user, **kwargs):
        payload = {
            "title": "My Playlist",
            "description": "desc",
            "youtube_playlist_id": "PL12345",
            "youtube_channel_title": "Channel",
            "youtube_item_count": 1,
            "youtube_privacy_status": "public",
            "sync_mode": "smart_diff",
            "sync_status": "idle",
        }
        payload.update(kwargs)
        return Playlist.objects.create(user=user, **payload)

    def make_item(self, playlist, **kwargs):
        payload = {
            "youtube_video_id": "vid1",
            "title": "Artist - Song (Official Video)",
            "channel_title": "Artist",
            "position": 0,
            "duration_seconds": 210,
            "is_removed_from_source": False,
        }
        payload.update(kwargs)
        return PlaylistItem.objects.create(playlist=playlist, **payload)

    def make_spotify_account(self, user, **kwargs):
        payload = {
            "spotify_user_id": "spotify-user-1",
            "access_token": "token",
            "refresh_token": "refresh",
            "expires_at": timezone.now() + timedelta(hours=1),
            "scope": "playlist-modify-public playlist-modify-private playlist-read-private user-read-private",
            "is_active": True,
        }
        payload.update(kwargs)
        return SpotifyAccount.objects.create(user=user, **payload)


class SpotifyMatchingServiceTests(TestCase, FactoryMixin):
    def test_clean_title_preserves_version_keywords(self):
        service = SpotifyMatchingService("token")
        cleaned = service.clean_title("Daft Punk - One More Time (Live Remix) [Official Video]")
        self.assertIn("Live Remix", cleaned)
        self.assertNotIn("Official Video", cleaned)

    @patch("apps.playlists.services.spotify_matching.SpotifyMatchingService.search_spotify")
    def test_match_item_fallback_search_and_threshold(self, mock_search):
        user = self.make_user()
        playlist = self.make_playlist(user)
        item = self.make_item(playlist, title="Coldplay - Yellow")

        mock_search.side_effect = [[], [
            {
                "id": "track123",
                "uri": "spotify:track:track123",
                "name": "Yellow",
                "artists": [{"name": "Coldplay"}],
                "duration_ms": 269000,
            }
        ]]

        service = SpotifyMatchingService("token")
        uri = service.match_item(item)

        self.assertEqual(uri, "spotify:track:track123")
        self.assertTrue(TrackMatch.objects.filter(playlist_item=item, is_active=True).exists())

    def test_search_spotify_rate_limit_fails_fast(self):
        service = SpotifyMatchingService("token")
        service.rate_limited_until = timezone.now().timestamp() + 30
        with self.assertRaises(SpotifyRateLimited):
            service.search_spotify("track:test", limit=5)


class YouTubeServiceTests(TestCase, FactoryMixin):
    def test_import_playlist_items_populates_duration(self):
        user = self.make_user()
        playlist = self.make_playlist(user, youtube_playlist_id="PL_IMPORT")

        service = YouTubeService.__new__(YouTubeService)
        service.client = MagicMock()
        service.MAX_ITEMS = 500

        request = MagicMock()
        request.execute.return_value = {
            "items": [
                {
                    "contentDetails": {"videoId": "video-1"},
                    "snippet": {
                        "title": "Track One",
                        "channelTitle": "Artist One",
                        "position": 0,
                        "thumbnails": {"high": {"url": "https://img/1.jpg"}},
                    },
                }
            ],
            "nextPageToken": None,
        }
        service.client.playlistItems.return_value.list.return_value = request

        service._execute_with_retry = MagicMock(side_effect=lambda req, operation: req.execute())
        service.get_video_durations = MagicMock(return_value={"video-1": 183})
        service.get_video_duration = MagicMock(return_value=183)

        created = service.import_playlist_items(playlist)

        self.assertEqual(created, 1)
        saved = PlaylistItem.objects.get(playlist=playlist, youtube_video_id="video-1")
        self.assertEqual(saved.duration_seconds, 183)


class PlaylistEndpointTests(APITestCase, FactoryMixin):
    def setUp(self):
        self.client = APIClient()
        self.user = self.make_user(email="api@example.com")
        self.client.force_authenticate(self.user)

    @patch("apps.playlists.views.YouTubeService.create_from_youtube")
    def test_create_playlist_endpoint(self, mock_create_from_youtube):
        playlist = self.make_playlist(self.user, youtube_playlist_id="PL_ENDPOINT")
        mock_create_from_youtube.return_value = playlist

        url = reverse("playlist-list")
        response = self.client.post(url, {"youtube_playlist_id": "PL_ENDPOINT"}, format="json")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["youtube_playlist_id"], "PL_ENDPOINT")

    @patch("apps.playlists.views.process_sync.delay")
    def test_trigger_sync_endpoint(self, mock_delay):
        playlist = self.make_playlist(self.user, youtube_playlist_id="PL_SYNC")

        url = reverse("playlist-sync", kwargs={"pk": playlist.id})
        response = self.client.post(url, {}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["status"], "queued")
        self.assertTrue(SyncOperation.objects.filter(playlist=playlist, status="queued").exists())
        mock_delay.assert_called_once()

    @patch("apps.playlists.views.process_sync.delay")
    def test_trigger_sync_returns_429_during_rate_limit_cooldown(self, mock_delay):
        playlist = self.make_playlist(self.user, youtube_playlist_id="PL_COOLDOWN")
        SyncOperation.objects.create(
            playlist=playlist,
            status="failed",
            ended_at=timezone.now(),
            errors={
                "summary": {
                    "rate_limited": True,
                    "retry_after_seconds": 60,
                }
            },
        )

        url = reverse("playlist-sync", kwargs={"pk": playlist.id})
        response = self.client.post(url, {}, format="json")

        self.assertEqual(response.status_code, 429)
        self.assertIn("retry_after_seconds", response.data)
        mock_delay.assert_not_called()

    @patch("apps.playlists.views.process_sync.delay")
    def test_trigger_sync_returns_429_for_account_wide_rate_limit_cooldown(self, mock_delay):
        playlist_a = self.make_playlist(self.user, youtube_playlist_id="PL_A")
        playlist_b = self.make_playlist(self.user, youtube_playlist_id="PL_B")

        SyncOperation.objects.create(
            playlist=playlist_a,
            status="failed",
            ended_at=timezone.now(),
            errors={
                "summary": {
                    "rate_limited": True,
                    "retry_after_seconds": 120,
                }
            },
        )

        url = reverse("playlist-sync", kwargs={"pk": playlist_b.id})
        response = self.client.post(url, {}, format="json")

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.data.get("scope"), "account")
        self.assertIn("retry_after_seconds", response.data)
        mock_delay.assert_not_called()


class ProcessSyncTaskTests(TestCase, FactoryMixin):
    @patch("apps.playlists.tasks.SpotifyService.remove_tracks")
    @patch("apps.playlists.tasks.SpotifyService.add_tracks")
    @patch("apps.playlists.tasks.SpotifyService.get_playlist_track_uris")
    @patch("apps.playlists.tasks.SpotifyService.create_playlist")
    @patch("apps.playlists.tasks.SpotifyMatchingService.match_item")
    @patch("apps.playlists.tasks.YouTubeService.resync_playlist")
    def test_process_sync_creates_playlist_and_adds_tracks(
        self,
        mock_resync,
        mock_match_item,
        mock_create_playlist,
        mock_get_track_uris,
        mock_add_tracks,
        mock_remove_tracks,
    ):
        user = self.make_user(email="task@example.com")
        self.make_spotify_account(user)
        playlist = self.make_playlist(user, spotify_playlist_id=None)
        item = self.make_item(playlist)
        operation = SyncOperation.objects.create(playlist=playlist, status="queued")

        mock_resync.return_value = {"status": "completed", "added": 0, "updated": 0, "removed": 0, "total": 1}
        mock_match_item.return_value = "spotify:track:track123"
        mock_create_playlist.return_value = {"id": "pl123", "uri": "spotify:playlist:pl123"}
        mock_get_track_uris.return_value = []
        mock_add_tracks.return_value = 1
        mock_remove_tracks.return_value = 0

        process_sync(operation.id)

        operation.refresh_from_db()
        playlist.refresh_from_db()

        self.assertEqual(operation.status, "completed")
        self.assertEqual(operation.matched_count, 1)
        self.assertEqual(operation.unmatched_count, 0)
        self.assertEqual(playlist.spotify_playlist_id, "pl123")
        self.assertEqual(playlist.sync_status, "success")
        mock_match_item.assert_called_once_with(item)

    @patch("apps.playlists.tasks.SpotifyService.remove_tracks")
    @patch("apps.playlists.tasks.SpotifyService.add_tracks")
    @patch("apps.playlists.tasks.SpotifyService.get_playlist_track_uris")
    @patch("apps.playlists.tasks.SpotifyService.create_playlist")
    @patch("apps.playlists.tasks.SpotifyMatchingService.match_item")
    @patch("apps.playlists.tasks.YouTubeService.resync_playlist")
    def test_smart_diff_preserves_manual_tracks(
        self,
        mock_resync,
        mock_match_item,
        mock_create_playlist,
        mock_get_track_uris,
        mock_add_tracks,
        mock_remove_tracks,
    ):
        user = self.make_user(email="diff@example.com")
        self.make_spotify_account(user)
        playlist = self.make_playlist(
            user,
            youtube_playlist_id="PL_DIFF",
            spotify_playlist_id="sp-pl-1",
            sync_mode="smart_diff",
        )
        item = self.make_item(playlist, youtube_video_id="vid-diff")
        removed_item = self.make_item(
            playlist,
            youtube_video_id="vid-old",
            title="Old Artist - Old Song",
            is_removed_from_source=True,
            position=1,
        )

        TrackMatch.objects.create(
            playlist_item=item,
            spotify_track_id="desired",
            spotify_track_uri="spotify:track:desired",
            confidence_score=0.9,
            is_active=True,
            match_method="auto_fuzzy",
        )
        TrackMatch.objects.create(
            playlist_item=removed_item,
            spotify_track_id="managed-old",
            spotify_track_uri="spotify:track:managed-old",
            confidence_score=0.8,
            is_active=True,
            match_method="auto_fuzzy",
        )

        operation = SyncOperation.objects.create(playlist=playlist, status="queued")

        mock_resync.return_value = {"status": "completed", "added": 0, "updated": 0, "removed": 0, "total": 1}
        mock_match_item.return_value = "spotify:track:desired"
        mock_create_playlist.return_value = {"id": "sp-pl-recreated", "uri": "spotify:playlist:sp-pl-recreated"}
        mock_get_track_uris.return_value = [
            "spotify:track:desired",
            "spotify:track:managed-old",
            "spotify:track:manual-user",
        ]
        mock_add_tracks.return_value = 0
        mock_remove_tracks.return_value = 1

        process_sync(operation.id)

        remove_args = mock_remove_tracks.call_args[0]
        self.assertEqual(remove_args[0], "sp-pl-1")
        self.assertEqual(remove_args[1], ["spotify:track:managed-old"])

    @patch("apps.playlists.tasks.SpotifyMatchingService.match_item")
    @patch("apps.playlists.tasks.YouTubeService.resync_playlist")
    def test_process_sync_marks_account_inactive_on_unauthorized(self, mock_resync, mock_match_item):
        user = self.make_user(email="unauth@example.com")
        account = self.make_spotify_account(user)
        playlist = self.make_playlist(user, spotify_playlist_id="sp-pl-unauth")
        self.make_item(playlist)
        operation = SyncOperation.objects.create(playlist=playlist, status="queued")

        mock_resync.return_value = {"status": "completed", "added": 0, "updated": 0, "removed": 0, "total": 1}
        mock_match_item.side_effect = UnauthorizedAPIError("token invalid")

        process_sync(operation.id)

        operation.refresh_from_db()
        account.refresh_from_db()
        playlist.refresh_from_db()

        self.assertEqual(operation.status, "failed")
        self.assertFalse(account.is_active)
        self.assertEqual(playlist.sync_status, "failed")

    @patch("apps.playlists.tasks.SpotifyService.create_playlist")
    @patch("apps.playlists.tasks.SpotifyMatchingService.match_item")
    @patch("apps.playlists.tasks.YouTubeService.resync_playlist")
    def test_process_sync_does_not_create_empty_spotify_playlist_when_no_matches(
        self,
        mock_resync,
        mock_match_item,
        mock_create_playlist,
    ):
        user = self.make_user(email="nomatch@example.com")
        self.make_spotify_account(user)
        playlist = self.make_playlist(user, spotify_playlist_id=None)
        self.make_item(playlist, youtube_video_id="vid-no-match")
        operation = SyncOperation.objects.create(playlist=playlist, status="queued")

        mock_resync.return_value = {"status": "completed", "added": 0, "updated": 0, "removed": 0, "total": 1}
        mock_match_item.return_value = None

        process_sync(operation.id)

        operation.refresh_from_db()
        playlist.refresh_from_db()

        self.assertEqual(operation.status, "partial")
        self.assertEqual(operation.matched_count, 0)
        self.assertIsNone(playlist.spotify_playlist_id)
        mock_create_playlist.assert_not_called()

    @patch("apps.playlists.tasks.SpotifyService.remove_tracks")
    @patch("apps.playlists.tasks.SpotifyService.add_tracks")
    @patch("apps.playlists.tasks.SpotifyService.get_playlist_track_uris")
    @patch("apps.playlists.tasks.SpotifyService.create_playlist")
    @patch("apps.playlists.tasks.SpotifyMatchingService.match_item")
    @patch("apps.playlists.tasks.YouTubeService.resync_playlist")
    def test_process_sync_recreates_playlist_when_existing_playlist_read_is_forbidden(
        self,
        mock_resync,
        mock_match_item,
        mock_create_playlist,
        mock_get_track_uris,
        mock_add_tracks,
        mock_remove_tracks,
    ):
        user = self.make_user(email="forbidden-read@example.com")
        self.make_spotify_account(user)
        playlist = self.make_playlist(user, spotify_playlist_id="sp-pl-old", spotify_playlist_uri="spotify:playlist:sp-pl-old")
        self.make_item(playlist, youtube_video_id="vid-f1")
        operation = SyncOperation.objects.create(playlist=playlist, status="queued")

        mock_resync.return_value = {"status": "completed", "added": 0, "updated": 0, "removed": 0, "total": 1}
        mock_match_item.return_value = "spotify:track:new-track"
        mock_get_track_uris.side_effect = ForbiddenAPIError("forbidden read")
        mock_create_playlist.return_value = {"id": "sp-pl-new", "uri": "spotify:playlist:sp-pl-new"}
        mock_add_tracks.return_value = 1
        mock_remove_tracks.return_value = 0

        process_sync(operation.id)

        playlist.refresh_from_db()
        operation.refresh_from_db()

        self.assertEqual(playlist.spotify_playlist_id, "sp-pl-new")
        self.assertEqual(operation.status, "partial")
        add_args = mock_add_tracks.call_args[0]
        self.assertEqual(add_args[0], "sp-pl-new")

    @patch("apps.playlists.tasks.SpotifyService.create_playlist")
    @patch("apps.playlists.tasks.SpotifyMatchingService.match_item")
    @patch("apps.playlists.tasks.YouTubeService.resync_playlist")
    def test_process_sync_aborts_fast_on_rate_limit(
        self,
        mock_resync,
        mock_match_item,
        mock_create_playlist,
    ):
        user = self.make_user(email="ratelimit@example.com")
        self.make_spotify_account(user)
        playlist = self.make_playlist(user, spotify_playlist_id=None)
        self.make_item(playlist, youtube_video_id="vid-r1")
        self.make_item(playlist, youtube_video_id="vid-r2", position=1)
        operation = SyncOperation.objects.create(playlist=playlist, status="queued")

        mock_resync.return_value = {"status": "completed", "added": 0, "updated": 0, "removed": 0, "total": 2}
        mock_match_item.side_effect = SpotifyRateLimited("Spotify rate-limited. Retry after 30.0s.")

        process_sync(operation.id)

        operation.refresh_from_db()
        playlist.refresh_from_db()

        self.assertEqual(operation.status, "failed")
        self.assertEqual(operation.matched_count, 0)
        self.assertEqual(operation.unmatched_count, 1)
        self.assertEqual(playlist.sync_status, "failed")
        mock_create_playlist.assert_not_called()
