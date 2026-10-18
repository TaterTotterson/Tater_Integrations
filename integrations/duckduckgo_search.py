from __future__ import annotations

__version__ = "1.1.0"

import os
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

from helpers import redis_client


DUCKDUCKGO_SEARCH_SETTINGS_KEY = "duckduckgo_search_settings"
DUCKDUCKGO_SEARCH_DEFAULT_REGION = "wt-wt"
DUCKDUCKGO_SEARCH_DEFAULT_TIMEOUT_SECONDS = 15
DUCKDUCKGO_SEARCH_HTML_URL = "https://html.duckduckgo.com/html/"
DUCKDUCKGO_SEARCH_MAX_RESPONSE_BYTES = int(os.getenv("TATER_WEB_SEARCH_MAX_RESPONSE_BYTES", "2000000"))
DUCKDUCKGO_SEARCH_MAX_SNIPPET_CHARS = int(os.getenv("TATER_WEB_SEARCH_MAX_SNIPPET_CHARS", "600"))
DUCKDUCKGO_SEARCH_MAX_PAGES = 10

INTEGRATION = {
    "id": "duckduckgo_search",
    "name": "DuckDuckGo Search",
    "description": "Adds keyless DuckDuckGo web search as a downloadable provider for Tater.",
    "badge": "DDG",
    "order": 14,
    "capabilities": ["web_search"],
    "fields": [
        {
            "key": "duckduckgo_region",
            "label": "DuckDuckGo Region",
            "type": "text",
            "default": DUCKDUCKGO_SEARCH_DEFAULT_REGION,
            "placeholder": "us-en",
            "description": "Search locale such as us-en, uk-en, de-de, or wt-wt for worldwide.",
        },
    ],
    "actions": [
        {"id": "test", "label": "Test Search", "status": "Runs one DuckDuckGo search query."},
    ],
}


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="ignore").strip()
    return str(value or "").strip()


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(_text(value)))
    except Exception:
        parsed = int(default)
    return max(int(minimum), min(int(maximum), parsed))


def _read_hash(key: str) -> Dict[str, Any]:
    try:
        raw = redis_client.hgetall(key) or {}
    except Exception:
        raw = {}
    return raw if isinstance(raw, dict) else {}


def _normalize_region(value: Any) -> str:
    region = _text(value).lower().replace("_", "-")
    if re.fullmatch(r"[a-z]{2}-[a-z]{2}", region):
        return region
    return DUCKDUCKGO_SEARCH_DEFAULT_REGION


def read_duckduckgo_search_settings() -> Dict[str, str]:
    current = _read_hash(DUCKDUCKGO_SEARCH_SETTINGS_KEY)
    region = (
        _text(current.get("DUCKDUCKGO_REGION") or current.get("duckduckgo_region"))
        or _text(os.getenv("TATER_WEB_SEARCH_DUCKDUCKGO_REGION") or os.getenv("DUCKDUCKGO_REGION"))
        or DUCKDUCKGO_SEARCH_DEFAULT_REGION
    )
    return {"DUCKDUCKGO_REGION": _normalize_region(region)}


def save_duckduckgo_search_settings(*, region: Any = None) -> Dict[str, str]:
    current = read_duckduckgo_search_settings()
    next_region = current.get("DUCKDUCKGO_REGION") if region is None else region
    redis_client.hset(
        DUCKDUCKGO_SEARCH_SETTINGS_KEY,
        mapping={"DUCKDUCKGO_REGION": _normalize_region(next_region)},
    )
    return read_duckduckgo_search_settings()


def read_integration_settings() -> Dict[str, Any]:
    settings = read_duckduckgo_search_settings()
    return {"duckduckgo_region": settings.get("DUCKDUCKGO_REGION", DUCKDUCKGO_SEARCH_DEFAULT_REGION)}


def save_integration_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    saved = save_duckduckgo_search_settings(region=(payload or {}).get("duckduckgo_region"))
    return {"duckduckgo_region": saved.get("DUCKDUCKGO_REGION", DUCKDUCKGO_SEARCH_DEFAULT_REGION)}


def integration_status() -> Dict[str, Any]:
    return {
        "configured": True,
        "message": "DuckDuckGo Search is ready. No API key or additional package is required.",
    }


def _query_with_site(query: str, site: Any) -> str:
    site_val = _text(site)
    return f"site:{site_val} {query}" if site_val else query


def _truncate_snippet(value: Any) -> str:
    snippet = _text(value)
    if len(snippet) > DUCKDUCKGO_SEARCH_MAX_SNIPPET_CHARS:
        snippet = snippet[:DUCKDUCKGO_SEARCH_MAX_SNIPPET_CHARS].rstrip() + "..."
    return snippet


def _display_url(url: str) -> str:
    parsed = urllib.parse.urlparse(_text(url))
    return parsed.netloc or _text(url)


_COUNTRY_DEFAULT_LANGUAGES = {
    "au": "en",
    "br": "pt",
    "ca": "en",
    "de": "de",
    "es": "es",
    "fr": "fr",
    "gb": "en",
    "ie": "en",
    "in": "en",
    "it": "it",
    "jp": "jp",
    "mx": "es",
    "nl": "nl",
    "nz": "en",
    "us": "en",
}


def _search_region(*, configured_region: Any, country: Any, language: Any) -> str:
    region = _normalize_region(configured_region)
    country_val = _text(country).lower()
    language_val = _text(language).lower()
    if language_val.startswith("lang_"):
        language_val = language_val[5:]
    if not re.fullmatch(r"[a-z]{2}", country_val or ""):
        country_val = ""
    if not re.fullmatch(r"[a-z]{2}", language_val or ""):
        language_val = ""
    if country_val:
        region_country = "uk" if country_val == "gb" else country_val
        region_language = language_val or _COUNTRY_DEFAULT_LANGUAGES.get(country_val, "en")
        return f"{region_country}-{region_language}"
    if language_val and not region.startswith("wt-"):
        return f"{region.split('-', 1)[0]}-{language_val}"
    return region


def _success(
    *,
    query: str,
    start_index: int,
    max_results: int,
    results: List[Dict[str, Any]],
    site_val: str,
    has_more: bool,
) -> Dict[str, Any]:
    next_start = start_index + len(results) if has_more and results else None
    return {
        "tool": "search_web",
        "ok": True,
        "provider": "duckduckgo_search",
        "provider_label": "DuckDuckGo Search",
        "query": query,
        "start": start_index,
        "count": len(results),
        "num_results": max_results,
        "results": results,
        "site_filter": site_val or None,
        "search_time_sec": None,
        "total_results": None,
        "has_more": bool(next_start),
        "next_start": next_start,
    }


def _error(message: str, *, needs: Optional[List[str]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "tool": "search_web",
        "ok": False,
        "provider": "duckduckgo_search",
        "provider_label": "DuckDuckGo Search",
        "error": message,
    }
    if needs:
        out["needs"] = needs
    return out


def _collapse_whitespace(value: Any) -> str:
    return " ".join(_text(value).split())


class _DuckDuckGoHTMLParser(HTMLParser):
    """Extract organic results and the next-page form from DuckDuckGo HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: List[Dict[str, str]] = []
        self.next_params: Optional[Dict[str, str]] = None
        self._active_result: Optional[Dict[str, str]] = None
        self._capture: Optional[str] = None
        self._form: Optional[Dict[str, str]] = None
        self._form_is_next = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        classes = set(attributes.get("class", "").split())

        if tag == "a" and "result__a" in classes:
            result = {"title": "", "href": attributes.get("href", ""), "body": ""}
            self.results.append(result)
            self._active_result = result
            self._capture = "title"
        elif tag == "a" and "result__snippet" in classes and self._active_result is not None:
            self._capture = "body"
        elif tag == "form":
            self._form = {}
            self._form_is_next = False
        elif tag == "input" and self._form is not None:
            name = attributes.get("name", "")
            if name:
                self._form[name] = attributes.get("value", "")
            if attributes.get("type", "").lower() == "submit" and attributes.get("value", "").lower() == "next":
                self._form_is_next = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._capture = None
        elif tag == "form" and self._form is not None:
            if self._form_is_next:
                self.next_params = dict(self._form)
            self._form = None
            self._form_is_next = False

    def handle_data(self, data: str) -> None:
        if self._active_result is not None and self._capture:
            self._active_result[self._capture] += data


def _normalize_result_url(value: Any) -> str:
    href = _text(value)
    if not href:
        return ""
    absolute = urllib.parse.urljoin("https://duckduckgo.com/", href)
    parsed = urllib.parse.urlparse(absolute)
    hostname = (parsed.hostname or "").lower()
    is_duckduckgo = hostname == "duckduckgo.com" or hostname.endswith(".duckduckgo.com")
    if is_duckduckgo and parsed.path.rstrip("/") == "/l":
        redirect_target = (urllib.parse.parse_qs(parsed.query).get("uddg") or [""])[0]
        absolute = _text(redirect_target)
        parsed = urllib.parse.urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return absolute


def _fetch_search_page(payload: Dict[str, str], *, timeout_sec: int) -> str:
    request = urllib.request.Request(
        DUCKDUCKGO_SEARCH_HTML_URL,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://html.duckduckgo.com/",
            "User-Agent": "Mozilla/5.0 (compatible; Tater/1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            raw = response.read(DUCKDUCKGO_SEARCH_MAX_RESPONSE_BYTES + 1)
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"DuckDuckGo returned HTTP {exc.code}.") from exc
    except Exception as exc:
        raise RuntimeError(f"DuckDuckGo request failed: {_text(exc) or exc.__class__.__name__}") from exc
    if len(raw) > DUCKDUCKGO_SEARCH_MAX_RESPONSE_BYTES:
        raise RuntimeError("DuckDuckGo returned an unexpectedly large response.")
    return raw.decode(charset, errors="replace")


def _raw_search(
    *,
    query: str,
    region: str,
    safesearch: str,
    max_results: int,
    timeout_sec: int,
) -> List[Dict[str, Any]]:
    safe_param = "-2" if safesearch == "off" else "-1"
    payload = {"q": query, "kl": region, "kp": safe_param}
    results: List[Dict[str, Any]] = []
    seen_urls = set()

    for _page_number in range(DUCKDUCKGO_SEARCH_MAX_PAGES):
        document = _fetch_search_page(payload, timeout_sec=timeout_sec)
        lowered = document.lower()
        if "unfortunately, bots use duckduckgo too" in lowered or "anomaly-modal" in lowered:
            raise RuntimeError("DuckDuckGo temporarily blocked the search request; try again later.")

        parser = _DuckDuckGoHTMLParser()
        parser.feed(document)
        for row in parser.results:
            url = _normalize_result_url(row.get("href"))
            if not url or url in seen_urls or "ad_domain=" in _text(row.get("href")):
                continue
            seen_urls.add(url)
            results.append(
                {
                    "title": _collapse_whitespace(row.get("title")) or url,
                    "href": url,
                    "body": _collapse_whitespace(row.get("body")),
                }
            )
            if len(results) >= max_results:
                return results

        if not parser.next_params:
            break
        payload = dict(parser.next_params)
        payload.setdefault("q", query)
        payload.setdefault("kl", region)
        payload.setdefault("kp", safe_param)

    return results


def integration_web_search(
    query: Any,
    *,
    num_results: Any = 5,
    start: Any = 1,
    site: Any = None,
    safe: Any = "active",
    country: Any = None,
    language: Any = None,
    timeout_sec: Any = DUCKDUCKGO_SEARCH_DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    q = _text(query)
    if not q:
        return _error("query is required.")

    max_results = _bounded_int(num_results, default=5, minimum=1, maximum=10)
    start_index = _bounded_int(start, default=1, minimum=1, maximum=91)
    timeout_val = _bounded_int(
        timeout_sec,
        default=DUCKDUCKGO_SEARCH_DEFAULT_TIMEOUT_SECONDS,
        minimum=3,
        maximum=60,
    )
    safe_mode = _text(safe).lower()
    safesearch = "off" if safe_mode == "off" else "moderate"
    site_val = _text(site)
    settings = read_duckduckgo_search_settings()
    region = _search_region(
        configured_region=settings.get("DUCKDUCKGO_REGION"),
        country=country,
        language=language,
    )

    end_index = start_index - 1 + max_results
    fetch_count = min(100, end_index + 1)
    try:
        raw_results = _raw_search(
            query=_query_with_site(q, site_val),
            region=region,
            safesearch=safesearch,
            max_results=fetch_count,
            timeout_sec=timeout_val,
        )
    except Exception as exc:
        return _error(f"DuckDuckGo Search failed: {_text(exc) or exc.__class__.__name__}")

    normalized: List[Dict[str, Any]] = []
    for item in raw_results:
        url = _text(item.get("href") or item.get("url") or item.get("link"))
        if not url:
            continue
        normalized.append(
            {
                "title": _text(item.get("title")) or url,
                "url": url,
                "snippet": _truncate_snippet(item.get("body") or item.get("snippet") or item.get("description")),
                "display_url": _display_url(url),
            }
        )

    offset = start_index - 1
    selected = normalized[offset : offset + max_results]
    has_more = len(normalized) > offset + len(selected)
    return _success(
        query=q,
        start_index=start_index,
        max_results=max_results,
        results=selected,
        site_val=site_val,
        has_more=has_more,
    )


def run_integration_action(action_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if _text(action_id) != "test":
        raise KeyError(f"Unsupported DuckDuckGo Search action: {action_id}")
    query = _text((payload or {}).get("query")) or "tater search test"
    result = integration_web_search(
        query,
        num_results=1,
        timeout_sec=DUCKDUCKGO_SEARCH_DEFAULT_TIMEOUT_SECONDS,
    )
    if not result.get("ok"):
        raise RuntimeError(_text(result.get("error")) or "DuckDuckGo Search test failed.")
    return {
        "ok": True,
        "message": "DuckDuckGo Search returned a result.",
        "count": result.get("count", 0),
    }
