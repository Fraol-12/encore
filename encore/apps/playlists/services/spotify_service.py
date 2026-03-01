import logging

import requests

from .retry_utils import (
    ForbiddenAPIError,
    TransientAPIError,
    UnauthorizedAPIError,
    parse_retry_after,
    retry_with_backoff,
)

logger = logging.getLogger(__name__)


class SpotifyService:
    """Encapsulates Spotify playlist operations with retries and rate-limit handling."""

    BASE_URL = "https://api.spotify.com/v1"
    RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, access_token: str):
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *, expected_status: tuple[int, ...] = (200,), params=None, payload=None):
        url = f"{self.BASE_URL}{path}"

        def _do_request():
            resp = requests.request(
                method,
                url,
                headers=self._headers,
                params=params,
                json=payload,
                timeout=15,
            )

            if resp.status_code == 401:
                raise UnauthorizedAPIError("Spotify access token is invalid or revoked")

            if resp.status_code == 403:
                www_auth = resp.headers.get("WWW-Authenticate", "")
                auth_hint = f" WWW-Authenticate: {www_auth}" if www_auth else ""
                raise ForbiddenAPIError(
                    f"Spotify API error 403 for {method} {path}: {resp.text[:500]}{auth_hint}"
                )

            if resp.status_code in self.RETRYABLE_STATUS:
                raise TransientAPIError(
                    f"Spotify API returned {resp.status_code} for {method} {path}",
                    retry_after=parse_retry_after(resp.headers.get("Retry-After")),
                )

            if resp.status_code not in expected_status:
                www_auth = resp.headers.get("WWW-Authenticate", "")
                auth_hint = f" WWW-Authenticate: {www_auth}" if www_auth else ""
                raise RuntimeError(
                    f"Spotify API error {resp.status_code} for {method} {path}: "
                    f"{resp.text[:500]}{auth_hint}"
                )

            if resp.status_code == 204:
                return None

            if not resp.text:
                return None

            return resp.json()

        return retry_with_backoff(f"Spotify {method} {path}", _do_request)

    def get_current_user(self) -> dict:
        return self._request("GET", "/me", expected_status=(200,))

    def create_playlist(
        self,
        *,
        name: str,
        description: str = "",
        public: bool = False,
        spotify_user_id: str | None = None,
    ) -> dict:
        payload = {
            "name": name[:100] if name else "Encore Playlist",
            "description": (description or "")[:300],
            "public": public,
        }
        # `/me/playlists` avoids user-id mismatch and is the recommended endpoint.
        path = "/me/playlists"
        if spotify_user_id:
            path = f"/users/{spotify_user_id}/playlists"
        return self._request(
            "POST",
            path,
            expected_status=(201,),
            payload=payload,
        )

    def update_playlist(self, playlist_id: str, name: str, description: str = "") -> None:
        payload = {
            "name": name[:100] if name else "Encore Playlist",
            "description": (description or "")[:300],
        }
        self._request("PUT", f"/playlists/{playlist_id}", expected_status=(200,), payload=payload)

    def get_playlist_track_uris(self, playlist_id: str) -> list[str]:
        uris: list[str] = []
        offset = 0

        while True:
            data = self._request(
                "GET",
                f"/playlists/{playlist_id}/tracks",
                params={"fields": "items(track(uri)),total,next", "limit": 100, "offset": offset},
            )

            items = data.get("items", []) if data else []
            for item in items:
                track = item.get("track") or {}
                uri = track.get("uri")
                if uri:
                    uris.append(uri)

            if not data or not data.get("next"):
                break
            offset += 100

        return uris

    def add_tracks(self, playlist_id: str, uris: list[str]) -> int:
        if not uris:
            return 0

        added = 0
        for i in range(0, len(uris), 100):
            chunk = uris[i : i + 100]
            self._request(
                "POST",
                f"/playlists/{playlist_id}/tracks",
                expected_status=(201,),
                payload={"uris": chunk},
            )
            added += len(chunk)
        return added

    def remove_tracks(self, playlist_id: str, uris: list[str]) -> int:
        if not uris:
            return 0

        removed = 0
        # Spotify remove endpoint supports up to 100 objects per request.
        for i in range(0, len(uris), 100):
            chunk = uris[i : i + 100]
            tracks = [{"uri": uri} for uri in chunk]
            self._request(
                "DELETE",
                f"/playlists/{playlist_id}/tracks",
                expected_status=(200,),
                payload={"tracks": tracks},
            )
            removed += len(chunk)
        return removed
