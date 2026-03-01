import logging
import re
import time

import requests
from rapidfuzz import fuzz

from apps.playlists.models import PlaylistItem, TrackMatch

from .retry_utils import (
    TransientAPIError,
    UnauthorizedAPIError,
    parse_retry_after,
    retry_with_backoff,
)

logger = logging.getLogger(__name__)


class SpotifySearchUnavailable(RuntimeError):
    """Raised when Spotify search is temporarily unavailable (rate limit/5xx)."""


class SpotifyRateLimited(SpotifySearchUnavailable):
    """Raised when Spotify explicitly returns HTTP 429."""

    def __init__(self, message: str, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class SpotifyMatchingService:
    """Handles matching YouTube PlaylistItems to Spotify tracks."""

    SEARCH_URL = "https://api.spotify.com/v1/search"
    RETRYABLE_STATUS = {429, 500, 502, 503, 504}
    MIN_CONFIDENCE = 0.55
    MAX_RATE_LIMIT_COOLDOWN = 86400.0

    def __init__(self, access_token: str):
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        self.rate_limited_until = 0.0
        self._next_cooldown_log_at = 0.0
        self._next_request_at = 0.0
        self._query_cache: dict[tuple[str, int], list[dict]] = {}

    def _wait_for_slot(self) -> None:
        """Apply lightweight request pacing to reduce 429 pressure."""
        now = time.time()
        if now < self._next_request_at:
            time.sleep(self._next_request_at - now)
        self._next_request_at = time.time() + 0.25

    def clean_title(self, title: str) -> str:
        """
        Normalize YouTube title while preserving versioning cues like Live/Remix/feat.
        """
        if not title:
            return ""

        noise_terms = (
            "official video",
            "official audio",
            "lyrics",
            "lyric video",
            "visualizer",
            "vevo",
            "4k",
            "hd",
            "topic",
        )
        keep_terms = ("live", "remix", "acoustic", "feat", "ft", "version", "edit")

        def replace_bracket(match: re.Match[str]) -> str:
            inner = next((group for group in match.groups() if group is not None), "").strip()
            lowered = inner.lower()
            if any(term in lowered for term in keep_terms):
                return f" {inner} "
            if any(term in lowered for term in noise_terms):
                return " "
            return " "

        cleaned = re.sub(r"\[(.*?)\]|\((.*?)\)|\{(.*?)\}", replace_bracket, title)
        cleaned = re.sub(r"\b(official video|official audio|lyrics?|lyric video|visualizer|vevo|topic)\b", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        cleaned = re.sub(r"\s+-\s+", " - ", cleaned)
        return cleaned.strip(" -_")

    def _guess_artist_and_title(self, cleaned: str, channel_title: str | None = None) -> tuple[str | None, str]:
        separators = [" - ", " – ", " — ", " | ", ": "]
        for sep in separators:
            if sep in cleaned:
                left, right = cleaned.split(sep, 1)
                left = left.strip()
                right = right.strip()
                if left and right:
                    return left, right

        lower_cleaned = cleaned.lower()
        if " by " in lower_cleaned:
            title_guess, artist_guess = cleaned.rsplit(" by ", 1)
            title_guess = title_guess.strip()
            artist_guess = artist_guess.strip()
            if title_guess and artist_guess:
                return artist_guess, title_guess

        artist_guess = channel_title.strip() if channel_title else None
        return artist_guess, cleaned.strip()

    def search_spotify(self, query: str, limit: int = 10) -> list[dict]:
        """Search Spotify tracks with retry and rate-limit handling."""

        cache_key = (query, limit)
        cached = self._query_cache.get(cache_key)
        if cached is not None:
            return cached

        now = time.time()
        if now < self.rate_limited_until:
            remaining = self.rate_limited_until - now
            if now >= self._next_cooldown_log_at and remaining > 0:
                logger.warning(
                    "Spotify search cooling down: %.2fs remaining",
                    remaining,
                )
                self._next_cooldown_log_at = now + 5.0
            raise SpotifyRateLimited(
                f"Spotify rate-limited. Retry after {remaining:.2f}s.",
                retry_after_seconds=remaining,
            )

        def _search():
            self._wait_for_slot()
            resp = requests.get(
                self.SEARCH_URL,
                headers=self.headers,
                params={"q": query, "type": "track", "limit": limit, "market": "US"},
                timeout=15,
            )
            if resp.status_code == 401:
                raise UnauthorizedAPIError("Spotify token revoked or expired during search")
            if resp.status_code in self.RETRYABLE_STATUS:
                retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                if resp.status_code == 429:
                    cooldown = min(max(retry_after or 2.0, 2.0), self.MAX_RATE_LIMIT_COOLDOWN)
                    self.rate_limited_until = max(self.rate_limited_until, time.time() + cooldown)
                    self._next_cooldown_log_at = 0.0
                    logger.warning(
                        "Spotify 429 on search '%s' (Retry-After raw=%s parsed=%.2fs)",
                        query,
                        resp.headers.get("Retry-After"),
                        cooldown,
                    )
                    raise SpotifyRateLimited(
                        f"Spotify rate-limited on search '{query}'. Retry after {cooldown:.2f}s.",
                        retry_after_seconds=cooldown,
                    )
                raise TransientAPIError(
                    f"Spotify search failed ({resp.status_code})",
                    retry_after=retry_after,
                )
            if resp.status_code != 200:
                raise RuntimeError(f"Spotify search failed: {resp.status_code} - {resp.text[:500]}")
            return resp.json().get("tracks", {}).get("items", [])

        try:
            results = retry_with_backoff(
                f"Spotify search '{query}'",
                _search,
                max_attempts=3,
                base_delay=1.0,
                max_sleep_seconds=30.0,
            )
            self._query_cache[cache_key] = results
            return results
        except SpotifySearchUnavailable:
            raise
        except RuntimeError as exc:
            raise SpotifySearchUnavailable(
                f"Spotify search temporarily unavailable for query '{query}'"
            ) from exc

    def score_match(self, yt_title: str, yt_artist_guess: str | None, spotify_track: dict, yt_duration: int | None = None) -> float:
        """Compute confidence in [0.0, 1.0] for one candidate."""
        spotify_name = spotify_track.get("name", "")
        spotify_artists = spotify_track.get("artists") or []
        spotify_artist = spotify_artists[0].get("name", "") if spotify_artists else ""

        title_ratio = fuzz.ratio(yt_title.lower(), spotify_name.lower())
        title_token = fuzz.token_set_ratio(yt_title.lower(), spotify_name.lower())
        title_score = max(title_ratio, title_token)

        artist_score = 75
        if yt_artist_guess:
            artist_score = max(
                fuzz.partial_ratio(yt_artist_guess.lower(), spotify_artist.lower()),
                fuzz.token_set_ratio(yt_artist_guess.lower(), spotify_artist.lower()),
            )

        duration_score = 80
        if yt_duration is not None:
            spotify_duration = (spotify_track.get("duration_ms", 0) or 0) / 1000
            delta = abs(spotify_duration - yt_duration)
            if delta <= 2:
                duration_score = 100
            elif delta <= 5:
                duration_score = 95
            elif delta <= 10:
                duration_score = 85
            elif delta <= 20:
                duration_score = 70
            elif delta <= 40:
                duration_score = 55
            else:
                duration_score = max(20, 100 - (delta * 1.2))

        weighted = (title_score * 0.55) + (artist_score * 0.25) + (duration_score * 0.20)
        return max(0.0, min(1.0, weighted / 100.0))

    def _iter_candidate_scores(
        self,
        title_guess: str,
        artist_guess: str | None,
        cleaned_title: str,
        duration_seconds: int | None,
        candidates: list[dict],
    ) -> tuple[float, dict | None]:
        seen_ids: set[str] = set()
        best_score = 0.0
        best_track = None

        for track in candidates:
            track_id = track.get("id")
            if not track_id or track_id in seen_ids:
                continue
            seen_ids.add(track_id)

            score = self.score_match(title_guess, artist_guess, track, duration_seconds)
            if score < 0.65:
                # Broaden title comparison for noisy metadata.
                score = max(score, self.score_match(cleaned_title, artist_guess, track, duration_seconds))

            if score > best_score:
                best_score = score
                best_track = track

        return best_score, best_track

    def match_item(self, item: PlaylistItem) -> str | None:
        cleaned = self.clean_title(item.title)
        artist_guess, title_guess = self._guess_artist_and_title(cleaned, item.channel_title)
        existing_active = (
            TrackMatch.objects.filter(playlist_item=item, is_active=True)
            .order_by("-confidence_score", "-matched_at")
            .first()
        )

        candidates: list[dict] = []
        search_unavailable = False
        if title_guess:
            query = f"track:{title_guess}"
            if artist_guess:
                query += f" artist:{artist_guess}"
            try:
                candidates = self.search_spotify(query, limit=10)
            except SpotifyRateLimited:
                raise
            except SpotifySearchUnavailable as exc:
                logger.warning("Primary Spotify search unavailable for playlist item %s: %s", item.id, exc)
                search_unavailable = True

        if not candidates and title_guess:
            try:
                candidates = self.search_spotify(title_guess, limit=10)
            except SpotifyRateLimited:
                raise
            except SpotifySearchUnavailable as exc:
                logger.warning("Fallback Spotify search unavailable for playlist item %s: %s", item.id, exc)
                search_unavailable = True

        best_score, best_track = self._iter_candidate_scores(
            title_guess=title_guess or cleaned,
            artist_guess=artist_guess,
            cleaned_title=cleaned,
            duration_seconds=item.duration_seconds,
            candidates=candidates,
        )

        if best_score < 0.75 and cleaned:
            broad_candidates = []
            try:
                broad_candidates = self.search_spotify(cleaned, limit=15)
            except SpotifyRateLimited:
                raise
            except SpotifySearchUnavailable as exc:
                logger.warning("Broad Spotify search unavailable for playlist item %s: %s", item.id, exc)
                search_unavailable = True
            broad_score, broad_track = self._iter_candidate_scores(
                title_guess=title_guess or cleaned,
                artist_guess=artist_guess,
                cleaned_title=cleaned,
                duration_seconds=item.duration_seconds,
                candidates=broad_candidates,
            )
            if broad_score > best_score:
                best_score = broad_score
                best_track = broad_track

        if best_track is None or best_score < self.MIN_CONFIDENCE:
            if search_unavailable and existing_active and existing_active.spotify_track_uri:
                logger.info(
                    "Reusing existing active match for playlist item %s while search is unavailable",
                    item.id,
                )
                return existing_active.spotify_track_uri

            if search_unavailable:
                raise SpotifySearchUnavailable(
                    f"Spotify search unavailable while matching playlist item {item.id}"
                )

            TrackMatch.objects.filter(playlist_item=item, is_active=True).update(is_active=False)
            return None

        TrackMatch.objects.filter(playlist_item=item, is_active=True).update(is_active=False)
        TrackMatch.objects.update_or_create(
            playlist_item=item,
            spotify_track_id=best_track["id"],
            defaults={
                "spotify_track_uri": best_track["uri"],
                "confidence_score": best_score,
                "match_method": "auto_fuzzy",
                "is_active": True,
                "match_metadata": {
                    "spotify_track": best_track,
                    "matching": {
                        "cleaned_title": cleaned,
                        "artist_guess": artist_guess,
                        "score": best_score,
                    },
                },
            },
        )

        logger.info("Matched playlist item %s to Spotify track %s (score=%.3f)", item.id, best_track["id"], best_score)
        return best_track["uri"]
