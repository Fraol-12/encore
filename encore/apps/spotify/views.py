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
from .models import SpotifyAccount


class SpotifyLoginView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        state = secrets.token_urlsafe(32)
        request.session['spotify_oauth_state'] = state

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
            return Response({'error': error}, status=status.HTTP_400_BAD_REQUEST)

        if not code:
            return Response({'error': 'No code provided'}, status=status.HTTP_400_BAD_REQUEST)

        # Verify state (CSRF protection)
        session_state = request.session.get('spotify_oauth_state')
        if not session_state or state != session_state:
            return Response({'error': 'Invalid state parameter'}, status=status.HTTP_400_BAD_REQUEST)

        # Exchange code for tokens
        payload = {
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': settings.SPOTIFY_REDIRECT_URI,
            'client_id': settings.SPOTIFY_CLIENT_ID,
            'client_secret': settings.SPOTIFY_CLIENT_SECRET,
        }

        try:
            response = requests.post(settings.SPOTIFY_TOKEN_URL, data=payload)
            response.raise_for_status()
            tokens = response.json()

        except requests.RequestException as e:
            return Response({'error': f"Token exchange failed: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

        # Get user profile to get spotify_user_id
        headers = {'Authorization': f"Bearer {tokens['access_token']}"}
        me_response = requests.get('https://api.spotify.com/v1/me', headers=headers)
        me_response.raise_for_status()
        spotify_user = me_response.json()

        # Store or update SpotifyAccount
        account, created = SpotifyAccount.objects.update_or_create(
            user=request.user,
            defaults={
                'spotify_user_id': spotify_user['id'],
                'access_token': tokens['access_token'],
                'refresh_token': tokens.get('refresh_token'),
                'expires_at': timezone.now() + timedelta(seconds=tokens['expires_in']),
                'scope': tokens['scope'],
                'is_active': True,
            }
        )

        # Clean up session
        del request.session['spotify_oauth_state']

        # Redirect to frontend or return success
        return Response({
            'message': 'Spotify account linked successfully',
            'spotify_user_id': spotify_user['id']
        }, status=status.HTTP_200_OK)