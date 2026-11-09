"""Authenticated MediaViewer REST API client."""

from __future__ import annotations

from urllib.parse import urlsplit

import requests


class APIError(RuntimeError):
    pass


class MediaViewerClient:
    TIMEOUT = 30
    MAX_BATCH = 1000

    def __init__(self, api_url, username=None, password=None, token=None, timeout=TIMEOUT):
        if not isinstance(api_url, str) or not api_url:
            raise APIError("MediaViewer API URL is required")
        parsed = urlsplit(api_url)
        if (parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.query
                or parsed.fragment or parsed.username is not None or parsed.password is not None):
            raise APIError("MediaViewer API URL must be an HTTP(S) API base URL")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise APIError("HTTP is allowed only for loopback API URLs")
        self.base_url = api_url.rstrip("/") + "/"
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.auth = (username, password) if username and password and not token else None
        if token:
            self.session.headers["Authorization"] = f"Token {token}"
        self.timeout = timeout

    def _request(self, method, path, *, params=None, json=None):
        try:
            response = self.session.request(
                method, self.base_url + path.lstrip("/"), params=params, json=json,
                timeout=self.timeout, allow_redirects=False,
            )
            if response.is_redirect or response.is_permanent_redirect:
                raise APIError("MediaViewer API redirect refused")
            response.raise_for_status()
            try:
                data = response.json()
            except (ValueError, requests.exceptions.JSONDecodeError) as exc:
                raise APIError("MediaViewer API returned invalid JSON") from exc
            if not isinstance(data, dict):
                raise APIError("MediaViewer API response must be a JSON object")
            return data
        except APIError:
            raise
        except requests.RequestException as exc:
            # Do not expose request exceptions: they may include credentials or response content.
            raise APIError(f"MediaViewer API request failed ({exc.__class__.__name__})") from None

    def candidates(self, limit, bucket=None, prefix=None):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= self.MAX_BATCH:
            raise APIError(f"batch size must be between 1 and {self.MAX_BATCH}")
        params = {"limit": limit}
        if bucket is not None:
            params["bucket"] = bucket
        if prefix is not None:
            params["prefix"] = prefix
        data = self._request("GET", "migrations/candidates/", params=params)
        records = data.get("candidates")
        if data.get("version") != 1 or not isinstance(records, list) or len(records) > limit:
            raise APIError("MediaViewer API returned malformed or over-limit candidate inventory")
        return records

    def acquire_run(self, run_id):
        data = self._request("POST", "migrations/run/", json={"run_id": run_id, "writers_paused": True})
        if data.get("acquired") is not True:
            raise APIError("MediaViewer run ownership was not acquired")

    def active_run(self):
        data = self._request("GET", "migrations/run/")
        if "run_id" not in data:
            raise APIError("MediaViewer API returned malformed run ownership")
        return data

    def release_run(self, run_id):
        data = self._request("DELETE", "migrations/run/", json={"run_id": run_id})
        if data.get("released") is not True:
            raise APIError("MediaViewer run ownership was not released")

    def publish(self, parent_pk, manifest):
        data = self._request("POST", f"migrations/{parent_pk}/publish/", json=manifest)
        if data.get("published") is not True:
            raise APIError("MediaViewer API did not confirm publication")
