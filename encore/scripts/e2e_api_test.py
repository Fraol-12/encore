#!/usr/bin/env python3
"""
Encore end-to-end API verification suite.

Uses real HTTP requests against a running Encore backend and validates:
- auth/JWT
- Spotify OAuth link flow (manual browser step)
- YouTube playlist import
- background sync lifecycle
- edge cases (invalid playlist, duplicate sync)
- ownership isolation

Requirements:
- requests
- running Django API + Celery worker + Redis + Postgres
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from requests.exceptions import RequestException


TERMINAL_SYNC_STATUSES = {"completed", "partial", "failed"}


@dataclass
class TestResult:
    name: str
    status: str
    details: str


class TestFailure(Exception):
    pass


class EncoreE2ETestSuite:
    def __init__(self) -> None:
        self.base_url = os.getenv("ENCORE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
        self.timeout = int(os.getenv("ENCORE_HTTP_TIMEOUT", "30"))
        self.connect_timeout = int(os.getenv("ENCORE_CONNECT_TIMEOUT", "8"))
        self.poll_interval = int(os.getenv("ENCORE_SYNC_POLL_INTERVAL", "5"))
        self.poll_timeout = int(os.getenv("ENCORE_SYNC_POLL_TIMEOUT", "300"))
        self.warmup_timeout = int(os.getenv("ENCORE_API_WARMUP_TIMEOUT", "60"))

        self.email = os.getenv("ENCORE_TEST_EMAIL", "test@encore.local")
        self.password = os.getenv("ENCORE_TEST_PASSWORD", "Test123!")

        self.second_email = os.getenv("ENCORE_SECOND_EMAIL", "test2@encore.local")
        self.second_password = os.getenv("ENCORE_SECOND_PASSWORD", "Test123!")

        self.valid_playlist_id = os.getenv(
            "ENCORE_VALID_YT_PLAYLIST_ID", "PL4fGSI1pDJn6jXS_Tv_N9B8Z0HTRVJE0m"
        )
        self.invalid_playlist_id = os.getenv("ENCORE_INVALID_YT_PLAYLIST_ID", "PL_INVALID_12345")
        self.required_scopes = set(
            os.getenv(
                "ENCORE_REQUIRED_SPOTIFY_SCOPES",
                "playlist-modify-public playlist-modify-private playlist-read-private user-read-private",
            ).split()
        )

        self.manual_redirect_url = os.getenv("ENCORE_SPOTIFY_REDIRECT_URL", "")
        self.test_token_refresh = os.getenv("ENCORE_TEST_TOKEN_REFRESH", "false").lower() == "true"
        self.force_spotify_relink = os.getenv("ENCORE_FORCE_SPOTIFY_RELINK", "false").lower() == "true"

        self.session = requests.Session()
        self.results: list[TestResult] = []

        self.access_token = ""
        self.refresh_token = ""
        self.spotify_auth_url = ""

        self.playlist_id: int | None = None
        self.youtube_item_count: int | None = None
        self.total_items: int | None = None

        self.sync_id: int | None = None
        self.sync_payload: dict[str, Any] = {}

    # -------------------- core helpers --------------------
    def _fail(self, name: str, details: str, response: requests.Response | None = None) -> None:
        if response is not None:
            body = self._response_body_text(response)
            details = f"{details}\nHTTP {response.status_code}\nBody:\n{body}"
        raise TestFailure(f"[{name}] {details}")

    @staticmethod
    def _safe_json(response: requests.Response) -> dict[str, Any] | list[Any] | None:
        try:
            return response.json()
        except ValueError:
            return None

    def _response_body_text(self, response: requests.Response) -> str:
        payload = self._safe_json(response)
        if payload is not None:
            return json.dumps(payload, indent=2, ensure_ascii=True)
        return response.text

    @staticmethod
    def _extract_results(payload: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("results"), list):
            return payload["results"]
        return []

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected: int | tuple[int, ...],
        auth: bool = False,
        **kwargs: Any,
    ) -> requests.Response:
        headers = kwargs.pop("headers", {})
        allow_redirects = kwargs.pop("allow_redirects", False)
        if auth:
            if not self.access_token:
                raise TestFailure("No access token available for authenticated request")
            headers["Authorization"] = f"Bearer {self.access_token}"

        url = f"{self.base_url}{path}"
        try:
            response = self.session.request(
                method,
                url,
                headers=headers,
                timeout=(self.connect_timeout, self.timeout),
                allow_redirects=allow_redirects,
                **kwargs,
            )
        except RequestException as exc:
            raise TestFailure(
                f"HTTP request error for {method} {path}: {exc}. "
                "Check that API is up and reachable, and that you're using the correct base URL/protocol."
            ) from exc

        if response.status_code in (301, 302, 307, 308):
            location = response.headers.get("Location", "")
            if location.startswith("https://") and self.base_url.startswith("http://"):
                raise TestFailure(
                    f"Endpoint {path} redirected HTTP -> HTTPS ({location}). "
                    "Your server is likely running with SECURE_SSL_REDIRECT=True (production settings). "
                    "For local E2E use config.settings.local, or disable secure redirect, "
                    "or run behind real TLS and set ENCORE_BASE_URL to https://..."
                )

        expected_codes = (expected,) if isinstance(expected, int) else expected
        if response.status_code not in expected_codes:
            body = self._response_body_text(response)
            raise TestFailure(
                f"Unexpected status for {method} {path}: got {response.status_code}, expected {expected_codes}\n"
                f"Body:\n{body}"
            )
        return response

    def _wait_for_api(self) -> str:
        deadline = time.time() + self.warmup_timeout
        last_error = "no attempts yet"
        while time.time() < deadline:
            try:
                # Use register endpoint for preflight to avoid schema renderer dependencies.
                self._request("GET", "/api/register/", expected=(200, 405), allow_redirects=False)
                return "api endpoint reachable"
            except TestFailure as exc:
                last_error = str(exc)
                time.sleep(2)

        raise TestFailure(
            f"API preflight failed after {self.warmup_timeout}s. Last error: {last_error}"
        )

    def _run_test(self, name: str, func, *, critical: bool = True) -> bool:
        try:
            details = func()
            self.results.append(TestResult(name, "PASS", details))
            print(f"[PASS] {name} - {details}")
            return True
        except TestFailure as exc:
            self.results.append(TestResult(name, "FAIL", str(exc)))
            print(f"[FAIL] {name}\n{exc}\n")
            return not critical
        except Exception as exc:  # noqa: BLE001
            self.results.append(TestResult(name, "FAIL", f"Unhandled error: {exc}"))
            print(f"[FAIL] {name}\nUnhandled error: {exc}\n")
            return not critical

    # -------------------- test cases --------------------
    def test_01_register_user(self) -> str:
        payload = {"email": self.email, "password": self.password}
        response = self._request("POST", "/api/register/", expected=(201, 400), json=payload)
        data = self._safe_json(response) or {}

        if response.status_code == 201:
            if not isinstance(data, dict) or "id" not in data or data.get("email") != self.email:
                self._fail("test_01_register_user", "Registration response missing id/email", response)
            return f"user created id={data['id']}"

        if response.status_code == 400:
            return "user already exists (accepted)"

        self._fail("test_01_register_user", "Unexpected register result", response)
        return "unreachable"

    def _login(self, email: str, password: str) -> tuple[str, str]:
        response = self._request("POST", "/api/token/", expected=200, json={"email": email, "password": password})
        data = self._safe_json(response)
        if not isinstance(data, dict) or "access" not in data or "refresh" not in data:
            self._fail("login", "Token response missing access/refresh", response)
        return data["access"], data["refresh"]

    def test_02_login(self) -> str:
        self.access_token, self.refresh_token = self._login(self.email, self.password)
        return "access + refresh tokens acquired"

    def test_03_spotify_login_url(self) -> str:
        response = self._request("GET", "/api/spotify/login/", expected=200, auth=True)
        data = self._safe_json(response)
        if not isinstance(data, dict) or not data.get("auth_url"):
            self._fail("test_03_spotify_login_url", "No auth_url returned", response)
        self.spotify_auth_url = data["auth_url"]
        return "spotify auth_url returned"

    def _spotify_status(self) -> dict[str, Any]:
        response = self._request("GET", "/api/spotify/status/", expected=200, auth=True)
        data = self._safe_json(response)
        if isinstance(data, dict):
            return data
        return {"linked": False}

    def _spotify_is_linked(self) -> bool:
        return self._spotify_status().get("linked") is True

    def _missing_required_scopes(self, status_payload: dict[str, Any]) -> set[str]:
        scope_value = status_payload.get("scope") or ""
        granted = set(str(scope_value).split())
        return self.required_scopes - granted

    def test_04_manual_oauth_and_callback(self) -> str:
        status_payload = self._spotify_status()
        missing_scopes = self._missing_required_scopes(status_payload)

        if status_payload.get("linked") and not missing_scopes and not self.force_spotify_relink:
            return "spotify already linked; callback step skipped"

        print("\nMANUAL STEP REQUIRED")
        if status_payload.get("linked") and missing_scopes:
            print("Spotify is linked, but required scopes are missing.")
            print(f"Missing scopes: {', '.join(sorted(missing_scopes))}")
            print("A fresh re-link is required so sync can manage/read playlists.")
        print("1) Open this URL in your browser:")
        print(self.spotify_auth_url)
        print("2) Authorize Spotify.")
        print("3) If browser reaches callback and shows 'Spotify linked', type 'done'.")
        print("4) Otherwise copy the FULL redirect URL from the browser address bar and paste it.")

        redirect_url = self.manual_redirect_url.strip()
        if not redirect_url:
            redirect_url = input("Paste redirect URL (or type 'done' if already linked): ").strip()

        if not redirect_url or redirect_url.lower() in {"skip", "done"}:
            status_after = self._spotify_status()
            missing_after = self._missing_required_scopes(status_after)
            if status_after.get("linked") and not missing_after:
                return "manual callback skipped; spotify already linked with required scopes"
            raise TestFailure(
                "No redirect URL provided and spotify is not linked with required scopes. "
                "Paste full callback URL containing code/state."
            )

        parsed = urlparse(redirect_url)
        query_string = parsed.query if parsed.query else redirect_url
        query = parse_qs(query_string)

        # If user pasted only 'code=...&state=...' parse_qs handles it via query_string above.
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        error = (query.get("error") or [""])[0]

        if error:
            raise TestFailure(f"Spotify returned error during OAuth callback: {error}")

        if not code or not state:
            raise TestFailure(
                "Could not parse code/state from input. "
                "Paste full callback URL, or raw 'code=...&state=...'."
            )

        # If callback already succeeded in browser, avoid replaying single-use code/state.
        status_now = self._spotify_status()
        if status_now.get("linked") and not self._missing_required_scopes(status_now):
            return "spotify linked in browser callback; replay skipped"

        response = self._request(
            "GET",
            f"/api/spotify/callback/?code={code}&state={state}",
            expected=(200, 400),
        )
        data = self._safe_json(response)
        if response.status_code == 400:
            # Common when callback already consumed in browser. Accept if account is linked now.
            status_after = self._spotify_status()
            if status_after.get("linked") and not self._missing_required_scopes(status_after):
                if isinstance(data, dict):
                    return f"callback replay returned 400 ({data.get('error')}); spotify already linked with required scopes"
                return "callback replay returned 400; spotify already linked"
            self._fail("test_04_manual_oauth_and_callback", "Spotify callback failed", response)

        status_final = self._spotify_status()
        missing_final = self._missing_required_scopes(status_final)
        if missing_final:
            raise TestFailure(
                "Spotify linked but required scopes are still missing after callback: "
                f"{', '.join(sorted(missing_final))}"
            )

        if not isinstance(data, dict) or data.get("message") != "Spotify linked":
            if status_final.get("linked"):
                return "spotify linked confirmed by status endpoint"
            self._fail("test_04_manual_oauth_and_callback", "Spotify callback did not confirm linking", response)
        return "spotify callback succeeded"

    def test_05_spotify_link_status(self) -> str:
        response = self._request("GET", "/api/spotify/status/", expected=200, auth=True)
        data = self._safe_json(response)
        if not isinstance(data, dict) or data.get("linked") is not True:
            self._fail("test_05_spotify_link_status", "Spotify account is not linked", response)
        missing = self._missing_required_scopes(data)
        if missing:
            raise TestFailure(f"Spotify linked but missing required scopes: {', '.join(sorted(missing))}")
        return f"linked=True spotify_user_id={data.get('spotify_user_id')} scopes_ok"

    def test_06_import_valid_playlist(self) -> str:
        response = self._request(
            "POST",
            "/api/playlists/",
            expected=(201, 400),
            auth=True,
            json={"youtube_playlist_id": self.valid_playlist_id},
        )
        data = self._safe_json(response)

        if response.status_code == 201:
            if not isinstance(data, dict) or not data.get("id"):
                self._fail("test_06_import_valid_playlist", "Missing playlist id in creation response", response)
            self.playlist_id = int(data["id"])
            self.youtube_item_count = int(data.get("youtube_item_count", 0))
            return f"created playlist_id={self.playlist_id} items={self.youtube_item_count}"

        # already imported case: find it via list endpoint
        list_resp = self._request("GET", "/api/playlists/", expected=200, auth=True)
        payload = self._safe_json(list_resp)
        rows = self._extract_results(payload if payload is not None else [])
        for row in rows:
            if row.get("youtube_playlist_id") == self.valid_playlist_id:
                self.playlist_id = int(row["id"])
                self.youtube_item_count = int(row.get("youtube_item_count", 0))
                return f"playlist already existed, reused playlist_id={self.playlist_id}"

        self._fail("test_06_import_valid_playlist", "Playlist import failed and existing playlist not found", response)
        return "unreachable"

    def test_07_import_invalid_playlist(self) -> str:
        response = self._request(
            "POST",
            "/api/playlists/",
            expected=400,
            auth=True,
            json={"youtube_playlist_id": self.invalid_playlist_id},
        )
        return f"invalid playlist rejected: status={response.status_code}"

    def test_08_list_my_playlists(self) -> str:
        if self.playlist_id is None:
            raise TestFailure("playlist_id is missing")
        response = self._request("GET", "/api/playlists/", expected=200, auth=True)
        payload = self._safe_json(response)
        rows = self._extract_results(payload if payload is not None else [])
        if not any(int(row.get("id", -1)) == self.playlist_id for row in rows):
            self._fail("test_08_list_my_playlists", "Created playlist not found in list", response)
        return f"playlist_id={self.playlist_id} visible in listing"

    def test_09_get_single_playlist(self) -> str:
        if self.playlist_id is None:
            raise TestFailure("playlist_id is missing")
        response = self._request("GET", f"/api/playlists/{self.playlist_id}/", expected=200, auth=True)
        data = self._safe_json(response)
        if not isinstance(data, dict) or int(data.get("id", -1)) != self.playlist_id:
            self._fail("test_09_get_single_playlist", "Returned playlist id mismatch", response)
        self.youtube_item_count = int(data.get("youtube_item_count", 0))
        return f"loaded playlist_id={self.playlist_id}"

    def test_10_list_playlist_items(self) -> str:
        if self.playlist_id is None:
            raise TestFailure("playlist_id is missing")
        response = self._request("GET", f"/api/playlists/{self.playlist_id}/items/", expected=200, auth=True)
        data = self._safe_json(response)
        if not isinstance(data, list):
            self._fail("test_10_list_playlist_items", "Expected list of items", response)

        self.total_items = len(data)
        if self.youtube_item_count is not None and self.youtube_item_count != self.total_items:
            self._fail(
                "test_10_list_playlist_items",
                f"Item count mismatch: youtube_item_count={self.youtube_item_count}, items_endpoint={self.total_items}",
                response,
            )
        return f"items count={self.total_items}"

    def test_11_trigger_sync(self) -> str:
        if self.playlist_id is None:
            raise TestFailure("playlist_id is missing")
        response = self._request("POST", f"/api/playlists/{self.playlist_id}/sync/", expected=202, auth=True)
        data = self._safe_json(response)
        if not isinstance(data, dict) or not data.get("sync_operation_id"):
            self._fail("test_11_trigger_sync", "Missing sync_operation_id", response)
        self.sync_id = int(data["sync_operation_id"])
        return f"sync queued sync_id={self.sync_id}"

    def test_12_trigger_duplicate_sync(self) -> str:
        if self.playlist_id is None:
            raise TestFailure("playlist_id is missing")
        response = self._request("POST", f"/api/playlists/{self.playlist_id}/sync/", expected=409, auth=True)
        return f"duplicate sync correctly blocked ({response.status_code})"

    def _poll_sync(self, sync_id: int) -> dict[str, Any]:
        deadline = time.time() + self.poll_timeout
        last_payload: dict[str, Any] = {}

        while time.time() < deadline:
            response = self._request("GET", f"/api/sync-operations/{sync_id}/", expected=200, auth=True)
            payload = self._safe_json(response)
            if not isinstance(payload, dict):
                self._fail("poll_sync", "Sync payload is not an object", response)

            last_payload = payload
            status = str(payload.get("status", ""))
            print(f"[SYNC] id={sync_id} status={status} matched={payload.get('matched_count')} unmatched={payload.get('unmatched_count')} errors={payload.get('error_count')}")
            if status in TERMINAL_SYNC_STATUSES:
                return payload

            time.sleep(self.poll_interval)

        raise TestFailure(
            f"Sync operation {sync_id} did not reach terminal state within {self.poll_timeout}s. "
            f"Last payload: {json.dumps(last_payload, ensure_ascii=True)}"
        )

    def test_13_poll_sync_until_terminal(self) -> str:
        if self.sync_id is None:
            raise TestFailure("sync_id is missing")
        payload = self._poll_sync(self.sync_id)
        self.sync_payload = payload
        status = str(payload.get("status", ""))
        return f"terminal status={status}"

    def test_14_assert_matched_count(self) -> str:
        if not self.sync_payload:
            raise TestFailure("sync payload missing")

        status = str(self.sync_payload.get("status", ""))
        matched = int(self.sync_payload.get("matched_count", 0))

        if status not in TERMINAL_SYNC_STATUSES:
            raise TestFailure(f"Sync is not terminal: {status}")
        if matched <= 0:
            raise TestFailure(
                f"Expected matched_count > 0 for known good playlist, got {matched}. "
                f"Sync payload: {json.dumps(self.sync_payload, ensure_ascii=True)}"
            )
        return f"matched_count={matched} (>0)"

    def test_15_assert_partial_math_and_error_logging(self) -> str:
        if not self.sync_payload:
            raise TestFailure("sync payload missing")

        status = str(self.sync_payload.get("status", ""))
        matched = int(self.sync_payload.get("matched_count", 0))
        unmatched = int(self.sync_payload.get("unmatched_count", 0))
        error_count = int(self.sync_payload.get("error_count", 0))
        errors = self.sync_payload.get("errors")

        if status == "partial":
            if self.total_items is None:
                raise TestFailure("total_items missing for partial arithmetic check")
            expected = self.total_items - matched
            actual = unmatched + error_count
            if actual != expected:
                raise TestFailure(
                    f"Partial arithmetic failed: unmatched+error_count={actual}, expected={expected} "
                    f"(total_items={self.total_items}, matched={matched})"
                )
            if not errors:
                raise TestFailure("Partial sync must include errors payload")
            return "partial arithmetic and error logging validated"

        if status == "failed":
            if not errors:
                raise TestFailure("Failed sync must include errors payload")
            return "failed sync has errors payload"

        return "status completed, partial checks skipped"

    def test_16_optional_token_refresh(self) -> str:
        if not self.test_token_refresh:
            return "skipped (set ENCORE_TEST_TOKEN_REFRESH=true for manual verification)"

        print("\nTOKEN REFRESH MANUAL STEP")
        print("Set the SpotifyAccount.expires_at for this user to a past timestamp in DB, then press Enter.")
        print("Example (inside Django shell):")
        print(
            "from django.utils import timezone; from apps.users.models import SpotifyAccount; "
            f"a=SpotifyAccount.objects.select_related('user').get(user__email='{self.email}'); "
            "a.expires_at=timezone.now()-timezone.timedelta(hours=1); a.save()"
        )
        input("Press Enter when done...")

        if self.playlist_id is None:
            raise TestFailure("playlist_id missing")

        response = self._request("POST", f"/api/playlists/{self.playlist_id}/sync/", expected=202, auth=True)
        payload = self._safe_json(response)
        if not isinstance(payload, dict) or not payload.get("sync_operation_id"):
            self._fail("test_16_optional_token_refresh", "Missing sync_operation_id", response)

        refresh_sync_id = int(payload["sync_operation_id"])
        result = self._poll_sync(refresh_sync_id)
        if result.get("status") == "failed":
            raise TestFailure(
                "Token refresh scenario failed unexpectedly. Sync payload: "
                f"{json.dumps(result, ensure_ascii=True)}"
            )
        return f"token refresh scenario passed with status={result.get('status')}"

    def test_17_optional_resync(self) -> str:
        if self.playlist_id is None:
            raise TestFailure("playlist_id missing")
        pre_count = self.total_items if self.total_items is not None else -1

        response = self._request("POST", f"/api/playlists/{self.playlist_id}/sync/", expected=202, auth=True)
        data = self._safe_json(response)
        if not isinstance(data, dict) or not data.get("sync_operation_id"):
            self._fail("test_17_optional_resync", "Missing sync_operation_id", response)

        rsync_id = int(data["sync_operation_id"])
        rsync_payload = self._poll_sync(rsync_id)

        items_response = self._request("GET", f"/api/playlists/{self.playlist_id}/items/", expected=200, auth=True)
        items_payload = self._safe_json(items_response)
        if not isinstance(items_payload, list):
            self._fail("test_17_optional_resync", "Items endpoint returned non-list", items_response)

        post_count = len(items_payload)
        if pre_count >= 0 and pre_count != post_count:
            raise TestFailure(
                f"Re-sync changed item count unexpectedly: before={pre_count}, after={post_count}. "
                f"Resync payload: {json.dumps(rsync_payload, ensure_ascii=True)}"
            )

        return f"re-sync terminal status={rsync_payload.get('status')} item_count_unchanged={post_count}"

    def test_18_ownership_isolation(self) -> str:
        if self.playlist_id is None:
            raise TestFailure("playlist_id missing")

        # register second user (allow exists)
        self._request(
            "POST",
            "/api/register/",
            expected=(201, 400),
            json={"email": self.second_email, "password": self.second_password},
        )

        second_access, _ = self._login(self.second_email, self.second_password)
        response = self.session.get(
            f"{self.base_url}/api/playlists/{self.playlist_id}/",
            headers={"Authorization": f"Bearer {second_access}"},
            timeout=self.timeout,
        )
        if response.status_code not in (403, 404):
            self._fail(
                "test_18_ownership_isolation",
                f"Expected 403/404 for cross-user access, got {response.status_code}",
                response,
            )
        return f"cross-user access blocked with {response.status_code}"

    # -------------------- runner --------------------
    def print_summary(self) -> None:
        print("\n" + "=" * 110)
        print("TEST SUMMARY")
        print("=" * 110)

        headers = ["Test name", "Status", "Details"]
        rows = [[r.name, r.status, r.details] for r in self.results]

        col_widths = [
            max(len(headers[0]), *(len(row[0]) for row in rows)) if rows else len(headers[0]),
            max(len(headers[1]), *(len(row[1]) for row in rows)) if rows else len(headers[1]),
            max(len(headers[2]), *(len(row[2]) for row in rows)) if rows else len(headers[2]),
        ]

        def fmt_row(values: list[str]) -> str:
            return " | ".join(v.ljust(col_widths[i]) for i, v in enumerate(values))

        print(fmt_row(headers))
        print("-+-".join("-" * width for width in col_widths))
        for row in rows:
            print(fmt_row(row))

        passed = sum(1 for r in self.results if r.status == "PASS")
        failed = sum(1 for r in self.results if r.status == "FAIL")
        print("-" * 110)
        print(f"Passed: {passed} | Failed: {failed} | Total: {len(self.results)}")

    def run(self) -> int:
        tests = [
            ("00 API preflight", self._wait_for_api, True),
            ("01 Register user", self.test_01_register_user, True),
            ("02 Login", self.test_02_login, True),
            ("03 Spotify login URL", self.test_03_spotify_login_url, True),
            ("04 Manual OAuth + callback", self.test_04_manual_oauth_and_callback, True),
            ("05 Spotify linked status", self.test_05_spotify_link_status, True),
            ("06 Import valid playlist", self.test_06_import_valid_playlist, True),
            ("07 Import invalid playlist", self.test_07_import_invalid_playlist, True),
            ("08 List my playlists", self.test_08_list_my_playlists, True),
            ("09 Get single playlist", self.test_09_get_single_playlist, True),
            ("10 List playlist items", self.test_10_list_playlist_items, True),
            ("11 Trigger sync", self.test_11_trigger_sync, True),
            ("12 Trigger duplicate sync", self.test_12_trigger_duplicate_sync, True),
            ("13 Poll sync until terminal", self.test_13_poll_sync_until_terminal, True),
            ("14 Assert matched_count > 0", self.test_14_assert_matched_count, True),
            ("15 Assert partial math + errors", self.test_15_assert_partial_math_and_error_logging, True),
            ("16 Optional token refresh verification", self.test_16_optional_token_refresh, False),
            ("17 Optional re-sync", self.test_17_optional_resync, False),
            ("18 Ownership isolation", self.test_18_ownership_isolation, True),
        ]

        for name, fn, critical in tests:
            proceed = self._run_test(name, fn, critical=critical)
            if not proceed:
                print("Fail-fast activated. Stopping test execution.")
                break

        self.print_summary()

        any_fail = any(r.status == "FAIL" for r in self.results)
        return 1 if any_fail else 0


def main() -> int:
    print("Encore API E2E Test Suite")
    print(f"Base URL: {os.getenv('ENCORE_BASE_URL', 'http://127.0.0.1:8000')}")

    suite = EncoreE2ETestSuite()
    return suite.run()


if __name__ == "__main__":
    sys.exit(main())
