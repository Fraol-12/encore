import base64
import binascii
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.models import SpotifyAccount

CustomUser = get_user_model()

STATE_TTL_SECONDS = 600


class SpotifyLoginView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        nonce = secrets.token_urlsafe(32)
        state_raw = f"{request.user.id}:{nonce}"
        signer = signing.TimestampSigner(salt="spotify-oauth-state")
        signed_state = signer.sign(state_raw)
        state = base64.urlsafe_b64encode(signed_state.encode()).decode().rstrip("=")

        params = {
            "client_id": settings.SPOTIFY_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": settings.SPOTIFY_REDIRECT_URI,
            "scope": settings.SPOTIFY_SCOPES,
            "state": state,
            "show_dialog": "true",
        }
        auth_url = f"{settings.SPOTIFY_AUTH_URL}?{urlencode(params)}"
        return Response({"auth_url": auth_url}, status=status.HTTP_200_OK)


class SpotifyCallbackView(APIView):
    permission_classes = []
    authentication_classes = []

    def get(self, request):
        code = request.GET.get("code")
        state = request.GET.get("state")
        error = request.GET.get("error")

        if error:
            return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)

        if not code or not state:
            return Response({"error": "Missing parameters"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            signed_state = base64.urlsafe_b64decode(state + "===").decode()
            signer = signing.TimestampSigner(salt="spotify-oauth-state")
            decoded = signer.unsign(signed_state, max_age=STATE_TTL_SECONDS)
            user_id_str, _nonce = decoded.split(":", 1)
            user_id = int(user_id_str)
        except (
            ValueError,
            UnicodeDecodeError,
            binascii.Error,
            signing.BadSignature,
            signing.SignatureExpired,
        ):
            return Response({"error": "Invalid state"}, status=status.HTTP_400_BAD_REQUEST)

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.SPOTIFY_REDIRECT_URI,
            "client_id": settings.SPOTIFY_CLIENT_ID,
            "client_secret": settings.SPOTIFY_CLIENT_SECRET,
        }

        try:
            token_response = requests.post(settings.SPOTIFY_TOKEN_URL, data=payload, timeout=15)
        except requests.RequestException:
            return Response({"error": "Spotify token request failed"}, status=status.HTTP_502_BAD_GATEWAY)

        if token_response.status_code != 200:
            return Response(
                {"error": "Token exchange failed", "details": token_response.text[:300]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tokens = token_response.json()
        access_token = tokens.get("access_token")
        if not access_token:
            return Response({"error": "Token response missing access token"}, status=status.HTTP_400_BAD_REQUEST)
        granted_scope = (tokens.get("scope") or settings.SPOTIFY_SCOPES).strip()

        try:
            me_resp = requests.get(
                "https://api.spotify.com/v1/me",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15,
            )
        except requests.RequestException:
            return Response({"error": "Failed to fetch Spotify profile"}, status=status.HTTP_502_BAD_GATEWAY)

        if me_resp.status_code != 200:
            return Response(
                {"error": "Failed to fetch Spotify profile", "details": me_resp.text[:300]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        me = me_resp.json()

        try:
            user = CustomUser.objects.get(id=user_id)
        except CustomUser.DoesNotExist:
            return Response({"error": "User not found for OAuth state"}, status=status.HTTP_400_BAD_REQUEST)

        SpotifyAccount.objects.update_or_create(
            user=user,
            defaults={
                "spotify_user_id": me.get("id", ""),
                "access_token": access_token,
                "refresh_token": tokens.get("refresh_token"),
                "expires_at": timezone.now() + timedelta(seconds=tokens.get("expires_in", 3600)),
                "scope": granted_scope,
                "is_active": True,
            },
        )

        return Response(
            {
                "message": "Spotify linked",
                "spotify_user_id": me.get("id"),
                "scope": granted_scope,
            },
            status=status.HTTP_200_OK,
        )


class SpotifyStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            account = request.user.spotify_account
        except SpotifyAccount.DoesNotExist:
            return Response({"linked": False}, status=status.HTTP_200_OK)

        return Response(
            {
                "linked": True,
                "spotify_user_id": account.spotify_user_id,
                "scope": account.scope,
                "expires_at": account.expires_at,
                "is_active": account.is_active,
                "last_updated": account.updated_at,
            },
            status=status.HTTP_200_OK,
        )
