from django.conf import settings
from django.http import JsonResponse, HttpResponseRedirect
from django.urls import reverse
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
import requests
from urllib.parse import urlencode
import secrets
from apps.users.models import SpotifyAccount
from django.core.cache import cache 
import base64 
from django.contrib.auth import get_user_model  # << updated here
from django.utils import timezone 
from datetime import timedelta

CustomUser = get_user_model()  # << define CustomUser


class SpotifyLoginView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        state_raw = f"{request.user.id}:{secrets.token_urlsafe(32)}"
        state = base64.urlsafe_b64encode(state_raw.encode()).decode().rstrip('=')

        params = {
            'client_id': settings.SPOTIFY_CLIENT_ID,
            'response_type': 'code',
            'redirect_uri': settings.SPOTIFY_REDIRECT_URI,
            'scope': settings.SPOTIFY_SCOPES,
            'state': state,
            'show_dialog': 'true',
        }

        auth_url = f"{settings.SPOTIFY_AUTH_URL}?{urlencode(params)}"
        return JsonResponse({'auth_url': auth_url})
    



class SpotifyCallbackView(APIView):
    permission_classes = []

    def get(self, request):
        code = request.GET.get('code')
        state = request.GET.get('state')
        error = request.GET.get('error')

        if error:
            return Response({'error': error}, status=400)

        if not code or not state:
            return Response({'error': 'Missing parameters'}, status=400)

        # Decode state
        try:
            decoded = base64.urlsafe_b64decode(state + '===').decode()
            user_id_str, stored_state = decoded.split(':', 1)
            user_id = int(user_id_str)
        except:
            return Response({'error': 'Invalid state format'}, status=400)

        # Reconstruct original state (we only need to verify format)
        # In real code, store nonce in cache with user_id as key

        # Exchange code for tokens
        payload = {
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': settings.SPOTIFY_REDIRECT_URI,
            'client_id': settings.SPOTIFY_CLIENT_ID,
            'client_secret': settings.SPOTIFY_CLIENT_SECRET,
        }

        response = requests.post(settings.SPOTIFY_TOKEN_URL, data=payload)
        if response.status_code != 200:
            return Response({'error': 'Token exchange failed'}, status=400)

        tokens = response.json()

        # Get user profile
        headers = {'Authorization': f"Bearer {tokens['access_token']}"}
        me = requests.get('https://api.spotify.com/v1/me', headers=headers).json()

        # Store account
        user = CustomUser.objects.get(id=user_id)
        account, _ = SpotifyAccount.objects.update_or_create(
            user=user,
            defaults={
                'spotify_user_id': me['id'],
                'access_token': tokens['access_token'],
                'refresh_token': tokens.get('refresh_token'),
                'expires_at': timezone.now() + timedelta(seconds=tokens['expires_in']),
                'scope': tokens['scope'],
                'is_active': True,
            }
        )

        return Response({
            'message': 'Spotify linked',
            'spotify_user_id': me['id']
        }, status=200)
    
class SpotifyStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            account = request.user.spotify_account
            return Response({
                'linked': True,
                'spotify_user_id': account.spotify_user_id,
                'expires_at': account.expires_at,
                'is_active': account.is_active,
                'last_updated': account.updated_at
            })
        except SpotifyAccount.DoesNotExist:
            return Response({'linked': False})    