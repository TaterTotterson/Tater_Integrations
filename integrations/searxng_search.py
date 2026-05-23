from __future__ import annotations
__version__ = "1.0.0"

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from helpers import redis_client

SEARXNG_SEARCH_SETTINGS_KEY = "searxng_search_settings"
SEARXNG_SEARCH_DEFAULT_TIMEOUT_SECONDS = 15
SEARXNG_SEARCH_MAX_RESPONSE_BYTES = int(os.getenv("TATER_WEB_SEARCH_MAX_RESPONSE_BYTES", "2000000"))
SEARXNG_SEARCH_MAX_SNIPPET_CHARS = int(os.getenv("TATER_WEB_SEARCH_MAX_SNIPPET_CHARS", "600"))

INTEGRATION = {
    "id": "searxng_search",
    "name": "SearXNG",
    "description": "Adds a self-hosted SearXNG instance as a downloadable web search provider for Tater.",
    "badge": "SX",
    "order": 11,
    "capabilities": ["web_search"],
    "fields": [
        {
            "key": "searxng_url",
            "label": "SearXNG URL",
            "type": "text",
            "default": "",
            "placeholder": "https://search.example.com",
        },
        {
            "key": "searxng_api_key",
            "label": "SearXNG API Key",
            "type": "password",
            "default": "",
            "description": "Optional bearer token for protected instances.",
        },
    ],
    "actions": [
        {"id": "test", "label": "Test Search", "status": "Runs one SearXNG JSON search query."},
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


def read_searxng_search_settings() -> Dict[str, str]:
    current = _read_hash(SEARXNG_SEARCH_SETTINGS_KEY)
    try:
        legacy_url = redis_client.get("tater:web_search:searxng_url")
        legacy_api_key = redis_client.get("tater:web_search:searxng_api_key")
    except Exception:
        legacy_url = ""
        legacy_api_key = ""
    url = (
        _text(current.get("SEARXNG_URL") or current.get("searxng_url"))
        or _text(legacy_url)
        or _text(os.getenv("TATER_WEB_SEARCH_SEARXNG_URL") or os.getenv("SEARXNG_URL"))
    )
    api_key = (
        _text(current.get("SEARXNG_API_KEY") or current.get("searxng_api_key"))
        or _text(legacy_api_key)
        or _text(os.getenv("TATER_WEB_SEARCH_SEARXNG_API_KEY") or os.getenv("SEARXNG_API_KEY"))
    )
    return {"SEARXNG_URL": url, "SEARXNG_API_KEY": api_key}


def save_searxng_search_settings(*, url: Any = None, api_key: Any = None) -> Dict[str, str]:
    current = read_searxng_search_settings()
    next_settings = {
        "SEARXNG_URL": _text(current.get("SEARXNG_URL") if url is None else url),
        "SEARXNG_API_KEY": _text(current.get("SEARXNG_API_KEY") if api_key is None else api_key),
    }
    redis_client.hset(SEARXNG_SEARCH_SETTINGS_KEY, mapping=next_settings)
    return read_searxng_search_settings()


def read_integration_settings() -> Dict[str, Any]:
    settings = read_searxng_search_settings()
    return {
        "searxng_url": settings.get("SEARXNG_URL", ""),
        "searxng_api_key": settings.get("SEARXNG_API_KEY", ""),
    }


def save_integration_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    saved = save_searxng_search_settings(
        url=(payload or {}).get("searxng_url"),
        api_key=(payload or {}).get("searxng_api_key"),
    )
    return {
        "searxng_url": saved.get("SEARXNG_URL", ""),
        "searxng_api_key": saved.get("SEARXNG_API_KEY", ""),
    }


def integration_status() -> Dict[str, Any]:
    configured = bool(read_searxng_search_settings().get("SEARXNG_URL"))
    return {
        "configured": configured,
        "message": "SearXNG is configured." if configured else "SearXNG URL is required.",
    }


def _query_with_site(query: str, site: Any) -> str:
    site_val = _text(site)
    return f"site:{site_val} {query}" if site_val else query


def _truncate_snippet(value: Any) -> str:
    snippet = _text(value)
    if len(snippet) > SEARXNG_SEARCH_MAX_SNIPPET_CHARS:
        snippet = snippet[:SEARXNG_SEARCH_MAX_SNIPPET_CHARS].rstrip() + "..."
    return snippet


def _display_url(url: str) -> str:
    parsed = urllib.parse.urlparse(_text(url))
    return parsed.netloc or _text(url)


def _success(
    *,
    query: str,
    start_index: int,
    max_results: int,
    results: List[Dict[str, Any]],
    site_val: str,
) -> Dict[str, Any]:
    next_start = start_index + max_results if len(results) >= max_results else None
    return {
        "tool": "search_web",
        "ok": True,
        "provider": "searxng_search",
        "provider_label": "SearXNG",
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
        "provider": "searxng_search",
        "provider_label": "SearXNG",
        "error": message,
    }
    if needs:
        out["needs"] = needs
    return out


def _read_json(req: urllib.request.Request, *, timeout_val: int) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    try:
        with urllib.request.urlopen(req, timeout=timeout_val) as resp:
            raw = resp.read(SEARXNG_SEARCH_MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(1000).decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        return None, _error(f"SearXNG request failed ({exc.code}): {body or exc}")
    except Exception as exc:
        return None, _error(f"SearXNG search failed: {exc}")
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None, _error("Invalid response from SearXNG.")
    return payload if isinstance(payload, dict) else {"data": payload}, None


def integration_web_search(
    query: Any,
    *,
    num_results: Any = 5,
    start: Any = 1,
    site: Any = None,
    safe: Any = "active",
    country: Any = None,
    language: Any = None,
    timeout_sec: Any = SEARXNG_SEARCH_DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    del country
    q = _text(query)
    if not q:
        return _error("query is required.")
    settings = read_searxng_search_settings()
    if not settings.get("SEARXNG_URL"):
        return _error(
            "SearXNG is not configured.",
            needs=["Set SearXNG URL in the SearXNG integration settings."],
        )

    max_results = _bounded_int(num_results, default=5, minimum=1, maximum=10)
    start_index = _bounded_int(start, default=1, minimum=1, maximum=91)
    timeout_val = _bounded_int(timeout_sec, default=SEARXNG_SEARCH_DEFAULT_TIMEOUT_SECONDS, minimum=3, maximum=60)
    safe_mode = _text(safe).lower()
    if safe_mode not in {"active", "off"}:
        safe_mode = "active"
    language_val = _text(language).lower()
    site_val = _text(site)

    base_url = settings.get("SEARXNG_URL", "").rstrip("/")
    endpoint = base_url if base_url.endswith("/search") else f"{base_url}/search"
    page = max(1, ((start_index - 1) // max(1, max_results)) + 1)
    params: Dict[str, Any] = {
        "q": _query_with_site(q, site_val),
        "format": "json",
        "pageno": page,
        "safesearch": 1 if safe_mode == "active" else 0,
        "categories": "general",
    }
    if language_val:
        params["language"] = language_val

    headers = {"Accept": "application/json", "User-Agent": "Tater-AgentLab/1.0"}
    if settings.get("SEARXNG_API_KEY"):
        headers["Authorization"] = f"Bearer {settings.get('SEARXNG_API_KEY')}"
    req = urllib.request.Request(endpoint + "?" + urllib.parse.urlencode(params, doseq=True), headers=headers)
    payload, error = _read_json(req, timeout_val=timeout_val)
    if error:
        return error

    results: List[Dict[str, Any]] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = _text(item.get("url"))
        if not url:
            continue
        results.append(
            {
                "title": _text(item.get("title")) or url,
                "url": url,
                "snippet": _truncate_snippet(item.get("content") or item.get("snippet")),
                "display_url": _text(item.get("engine")) or _display_url(url),
            }
        )
        if len(results) >= max_results:
            break
    return _success(query=q, start_index=start_index, max_results=max_results, results=results, site_val=site_val)


def run_integration_action(action_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if _text(action_id) != "test":
        raise KeyError(f"Unsupported SearXNG action: {action_id}")
    query = _text((payload or {}).get("query")) or "tater search test"
    result = integration_web_search(query, num_results=1, timeout_sec=SEARXNG_SEARCH_DEFAULT_TIMEOUT_SECONDS)
    if not result.get("ok"):
        raise RuntimeError(_text(result.get("error")) or "SearXNG test failed.")
    return {"ok": True, "message": "SearXNG returned a result.", "count": result.get("count", 0)}
