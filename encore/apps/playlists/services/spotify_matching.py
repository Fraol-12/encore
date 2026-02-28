from rapidfuzz import fuzz, process
import requests
from django.conf import settings
from django.utils import timezone
from apps.playlists.models import TrackMatch, PlaylistItem 

class SpotifyMatchingService:
    """Handles matching YouTube PlaylistItems to Spotify tracks."""

    def __init__(self, access_token: str):
        self.headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }

    def clean_title(self, title: str) -> str:
        import re
        # Remove only the most obvious junk
        title = re.sub(r'\[.*?\]|\(.*?\)|\{.*?\}', '', title)  # brackets
        title = re.sub(r'(Official Video|Official Audio|Vevo|HD|4K|Music Video|Visualizer|Topic)', '', title, flags=re.I)
        title = re.sub(r'^\s*-\s*|\s*-\s*$', '', title)  # leading/trailing dashes
        return title.strip()
        

    def search_spotify(self, query: str, limit=10):
        """Search Spotify tracks."""
        params = {
            'q': query,
            'type': 'track',
            'limit': limit,
            'market': 'US'  # or user's country later
        }
        resp = requests.get('https://api.spotify.com/v1/search', headers=self.headers, params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"Spotify search failed: {resp.text}")
        return resp.json()['tracks']['items']

    def score_match(self, yt_title: str, yt_artist_guess: str, spotify_track, yt_duration: int = None):
        """Score a Spotify track candidate. yt_duration is optional."""
        title_score = fuzz.ratio(yt_title.lower(), spotify_track['name'].lower())
        artist_score = 0
        if yt_artist_guess:
            artist_score = fuzz.partial_ratio(yt_artist_guess.lower(), spotify_track['artists'][0]['name'].lower())

        duration_delta = 0
        if yt_duration:
            spotify_duration = spotify_track['duration_ms'] / 1000
            duration_delta = abs(spotify_duration - yt_duration)
            duration_penalty = max(0, 100 - duration_delta * 5)  # lose points per second
        else:
            duration_penalty = 80
        score = (title_score * 0.7) + (artist_score * 0.2) + (duration_penalty * 0.1)
        return score / 100  # 0.0 to 1.0
    

    def match_item(self, item: PlaylistItem):
        print(f"[MATCH] Processing item {item.id} - YT title: {item.title}")

        cleaned = self.clean_title(item.title)
        print(f"[MATCH] Cleaned title: '{cleaned}'")

        # Try to guess artist/title
        # Better splitting
        if ' - ' in cleaned:
            parts = cleaned.split(' - ', 1)
            artist_guess = parts[0].strip()
            title_guess = parts[1].strip()
        elif ' by ' in cleaned.lower():
            parts = cleaned.lower().split(' by ', 1)
            artist_guess = parts[1].strip()
            title_guess = parts[0].strip()
        else:
            artist_guess = None
            title_guess = cleaned

        query = f'track:{title_guess}'
        if artist_guess:
            query += f' artist:{artist_guess}'

        # Step 1: Try exact search
        query = f'track:"{title_guess}"'
        if artist_guess:
            query += f' artist:"{artist_guess}"'
        print(f"[MATCH] Exact search query: {query}")
        print(f"[MATCH DEBUG] Searching Spotify with query: '{query}'")
        candidates = self.search_spotify(query, limit=5)
        print(f"[MATCH] Exact search returned {len(candidates)} candidates")

        best_score = 0
        best_track = None
        best_method = 'exact'

        for track in candidates:
            score = self.score_match(title_guess, artist_guess, track, item.duration_seconds)
            print(f"[MATCH] Candidate '{track['name']}' by {track['artists'][0]['name']} - score: {score:.3f}")
            if score > best_score:
                best_score = score
                best_track = track

        # Step 2: Fallback broader search
        if best_score < 0.85:
            print("[MATCH] Low confidence — trying broader fuzzy search")
            candidates = self.search_spotify(title_guess, limit=10)
            print(f"[MATCH] Fuzzy search returned {len(candidates)} candidates")
            for track in candidates:
                score = self.score_match(cleaned, artist_guess, track, item.duration_seconds)
                print(f"[MATCH] Fuzzy candidate '{track['name']}' - score: {score:.3f}")
                if score > best_score:
                    best_score = score
                    best_track = track
                    best_method = 'fuzzy'

        print(f"[MATCH] Best score: {best_score:.3f}, method: {best_method}")

        if best_score < 0.6:  # Lowered threshold for debug
            print("[MATCH] Score too low — no match")
            return None

        # Store the match
        TrackMatch.objects.update_or_create(
            playlist_item=item,
            defaults={
                'spotify_track_id': best_track['id'],
                'spotify_track_uri': best_track['uri'],
                'confidence_score': best_score,
                'match_method': best_method,
                'is_active': True,
                'match_metadata': best_track
            }
        )
        print(f"[MATCH] Stored match with confidence {best_score:.3f}")

        return best_track['uri']