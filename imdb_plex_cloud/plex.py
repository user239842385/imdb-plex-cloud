"""Plex cloud transport. Tokens remain in memory; redirects are not followed."""

import json
import logging
import time
import uuid
import webbrowser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .core import IMDB_ID, PLEX_KEY, ImportProblem, number

METADATA = "https://metadata.provider.plex.tv"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PlexCloud:
    def __init__(self, token, requests_per_second=2, opener=None, sleeper=time.sleep):
        self._token = token
        self._opener = opener or build_opener(NoRedirect())
        self._sleep = sleeper
        self._interval = 1 / requests_per_second
        self._last_request = 0
        user = self._request("GET", "https://plex.tv/api/v2/user")
        if not user.get("id"):
            raise ImportProblem("Could not verify the signed-in Plex account.")
        self.account_id = str(user["id"])

    def _request(self, method, url, params=None):
        if url != "https://plex.tv/api/v2/user" and not url.startswith(METADATA + "/"):
            raise ImportProblem("Refusing an unexpected API destination.")
        if params:
            url += "?" + urlencode(params)
        headers = {"X-Plex-Token": self._token, "Accept": "application/json", "X-Plex-Product": "IMDb Plex Cloud Importer"}
        request = Request(url, method=method, headers=headers)
        for attempt in range(4):
            delay = self._interval - (time.monotonic() - self._last_request)
            if delay > 0:
                self._sleep(delay)
            self._last_request = time.monotonic()
            try:
                with self._opener.open(request, timeout=30) as response:
                    raw = response.read()
                    result = json.loads(raw) if raw else {}
                    if not isinstance(result, dict):
                        raise ValueError()
                    return result
            except HTTPError as error:
                if error.code not in {429, 500, 502, 503, 504} or attempt == 3:
                    raise ImportProblem(f"Plex returned HTTP {error.code}. No response body or credentials were logged.") from None
                try:
                    delay = min(60, max(1, float(error.headers.get("Retry-After", 2 ** attempt))))
                except (TypeError, ValueError):
                    delay = 2 ** attempt
                self._sleep(delay)
            except (URLError, TimeoutError, OSError):
                if attempt == 3:
                    raise ImportProblem("Plex network request failed. Check connectivity and try again.") from None
                self._sleep(2 ** attempt)
            except (ValueError, TypeError):
                raise ImportProblem("Plex returned an unexpected response format.") from None
        raise ImportProblem("Plex request failed.")

    def match(self, imdb_id, media_type):
        if not IMDB_ID.fullmatch(imdb_id) or media_type not in {"movie", "show"}:
            raise ImportProblem("Invalid lookup.")
        result = self._request("GET", METADATA + "/library/metadata/matches", {"guid": "imdb://" + imdb_id, "type": 2 if media_type == "show" else 1, "includeGuids": 1})
        matches = self._metadata(result)
        if len(matches) != 1:
            return {"status": "not_found" if not matches else "ambiguous"}
        match = matches[0]
        key = str(match.get("ratingKey") or match.get("guid", "").rsplit("/", 1)[-1])
        ids = [g.get("id", "") for g in match.get("Guid", []) if g.get("id", "").startswith("imdb://")]
        if match.get("type") != media_type or not PLEX_KEY.fullmatch(key):
            return {"status": "type_or_key_mismatch"}
        if "imdb://" + imdb_id not in ids:
            return {"status": "id_mismatch"}
        return {"status": "matched", "key": key, "plex_title": match.get("title", ""), "plex_year": match.get("year")}

    def get_rating(self, key):
        if not PLEX_KEY.fullmatch(key):
            raise ImportProblem("Invalid Plex metadata key.")
        result = self._request("GET", METADATA + "/library/metadata/" + key, {"includeUserState": 1})
        items = self._metadata(result)
        if len(items) != 1:
            raise ImportProblem("Could not verify the current online rating.")
        try:
            return number(items[0].get("userRating"))
        except (ValueError, TypeError):
            raise ImportProblem("Plex returned an invalid personal rating.") from None

    def set_rating(self, key, rating):
        if not PLEX_KEY.fullmatch(key):
            raise ImportProblem("Invalid Plex metadata key.")
        try:
            rating = number(rating)
        except (ValueError, TypeError):
            raise ImportProblem("Invalid rating.") from None
        self._request("PUT", METADATA + "/actions/rate", {"key": key, "identifier": "tv.plex.provider.metadata", "rating": -1 if rating is None else rating})

    @staticmethod
    def _metadata(result):
        container = result.get("MediaContainer")
        if not isinstance(container, dict):
            raise ImportProblem("Plex returned an unexpected metadata response.")
        items = container.get("Metadata", [])
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ImportProblem("Plex returned an unexpected metadata response.")
        for item in items:
            guids = item.get("Guid", [])
            if not isinstance(guids, list) or any(not isinstance(g, dict) or not isinstance(g.get("id", ""), str) for g in guids):
                raise ImportProblem("Plex returned unexpected identifiers.")
        return items


def sign_in(open_browser=True, requests_per_second=2):
    try:
        from plexapi.myplex import MyPlexPinLogin
    except ImportError:
        raise ImportProblem("Install this project first: python -m pip install .") from None
    # PlexAPI debug logs may include tokens. This tool does not enable them.
    logger = logging.getLogger("plexapi")
    logger.disabled = True
    try:
        pin = MyPlexPinLogin(headers={"X-Plex-Client-Identifier": str(uuid.uuid4()), "X-Plex-Product": "IMDb Plex Cloud Importer"}, oauth=True)
        url = pin.oauthUrl()
        print("Sign in at the following temporary Plex authorization link. Do not share it:\n" + url, flush=True)
        if open_browser:
            webbrowser.open(url)
        pin.run(timeout=300)
        pin.waitForLogin()
        if not pin.token:
            raise ImportProblem("Plex sign-in timed out. Run the command again.")
        return PlexCloud(pin.token, requests_per_second=requests_per_second)
    except ImportProblem:
        raise
    except Exception:
        raise ImportProblem("Plex sign-in failed. Check connectivity, approve the correct Plex account, and try again.") from None
