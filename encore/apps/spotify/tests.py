from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.users.models import CustomUser, SpotifyAccount


class SpotifyViewsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = CustomUser.objects.create_user(email="spotify@test.com", password="testpass123")

    def test_status_endpoint_unlinked(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(reverse("spotify-status"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["linked"])

    def test_status_endpoint_linked(self):
        SpotifyAccount.objects.create(
            user=self.user,
            spotify_user_id="abc",
            access_token="token",
            refresh_token="refresh",
            expires_at=timezone.now() + timedelta(hours=1),
            scope="playlist-modify-private",
            is_active=True,
        )
        self.client.force_authenticate(self.user)
        response = self.client.get(reverse("spotify-status"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["linked"])

    @patch("apps.spotify.views.requests.get")
    @patch("apps.spotify.views.requests.post")
    def test_callback_rejects_invalid_cached_state(self, mock_post, mock_get):
        cache.clear()

        response = self.client.get(reverse("spotify-callback"), {"code": "code123", "state": "bad"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.data)

        mock_post.assert_not_called()
        mock_get.assert_not_called()
