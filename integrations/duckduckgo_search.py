from __future__ import annotations

__version__ = "1.0.0"

import os
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from helpers import redis_client


DUCKDUCKGO_SEARCH_SETTINGS_KEY = "duckduckgo_search_settings"
DUCKDUCKGO_SEARCH_DEFAULT_REGION = "wt-wt"
DUCKDUCKGO_SEARCH_DEFAULT_TIMEOUT_SECONDS = 15
DUCKDUCKGO_SEARCH_MAX_SNIPPET_CHARS = int(os.getenv("TATER_WEB_SEARCH_MAX_SNIPPET_CHARS", "600"))

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


def _load_ddgs_client() -> Tuple[Any, str]:
    try:
        from ddgs import DDGS

        return DDGS, "ddgs"
    except Exception:
        try:
            from duckduckgo_search import DDGS

            return DDGS, "duckduckgo_search"
        except Exception as legacy_exc:
            raise RuntimeError(
                "DuckDuckGo search dependency is unavailable; install the ddgs Python package."
            ) from legacy_exc


def integration_status() -> Dict[str, Any]:
    try:
        _client, package_name = _load_ddgs_client()
    except Exception:
        return {
            "configured": False,
            "message": "DuckDuckGo Search needs the ddgs Python package installed in Tater.",
        }
    return {
        "configured": True,
        "message": f"DuckDuckGo Search is ready ({package_name}).",
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


def _proxy_url() -> Optional[str]:
    proxies = urllib.request.getproxies()
    return _text(proxies.get("https") or proxies.get("http")) or None


def _raw_search(
    *,
    query: str,
    region: str,
    safesearch: str,
    max_results: int,
    timeout_sec: int,
) -> List[Dict[str, Any]]:
    client_class, package_name = _load_ddgs_client()
    backend = "duckduckgo" if package_name == "ddgs" else "auto"
    with client_class(proxy=_proxy_url(), timeout=timeout_sec) as client:
        rows = client.text(
            query,
            region=region,
            safesearch=safesearch,
            max_results=max_results,
            backend=backend,
        )
    return [row for row in (rows or []) if isinstance(row, dict)]


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
    try:
        _load_ddgs_client()
    except Exception:
        return _error(
            "DuckDuckGo Search dependency is unavailable.",
            needs=["Install the ddgs Python package in Tater's runtime, then restart Tater."],
        )

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
