from __future__ import annotations
__version__ = "1.0.0"

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from helpers import redis_client

GOOGLE_SEARCH_SETTINGS_KEY = "google_search_settings"
GOOGLE_SEARCH_LEGACY_SETTINGS_KEY = "verba_settings:Web Search"
GOOGLE_SEARCH_API_URL = "https://www.googleapis.com/customsearch/v1"
GOOGLE_SEARCH_DEFAULT_TIMEOUT_SECONDS = 15
GOOGLE_SEARCH_MAX_RESPONSE_BYTES = int(os.getenv("TATER_WEB_SEARCH_MAX_RESPONSE_BYTES", "2000000"))
GOOGLE_SEARCH_MAX_SNIPPET_CHARS = int(os.getenv("TATER_WEB_SEARCH_MAX_SNIPPET_CHARS", "600"))

INTEGRATION = {
    "id": "google_search",
    "name": "Google Custom Search",
    "description": "Adds Google Custom Search as a downloadable web search provider for Tater.",
    "badge": "GOO",
    "order": 12,
    "capabilities": ["web_search"],
    "fields": [
        {"key": "google_api_key", "label": "Google API Key", "type": "password", "default": ""},
        {"key": "google_search_cx", "label": "Google Search CX", "type": "text", "default": ""},
    ],
    "actions": [
        {"id": "test", "label": "Test Search", "status": "Runs one Google Custom Search query."},
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


def read_google_search_settings() -> Dict[str, str]:
    current = _read_hash(GOOGLE_SEARCH_SETTINGS_KEY)
    legacy_hash = _read_hash(GOOGLE_SEARCH_LEGACY_SETTINGS_KEY)
    try:
        legacy_api_key = redis_client.get("tater:web_search:google_api_key")
        legacy_cx = redis_client.get("tater:web_search:google_cx")
    except Exception:
        legacy_api_key = ""
        legacy_cx = ""
    api_key = (
        _text(current.get("GOOGLE_API_KEY") or current.get("google_api_key"))
        or _text(legacy_api_key)
        or _text(legacy_hash.get("GOOGLE_API_KEY") or legacy_hash.get("google_api_key"))
        or _text(os.getenv("TATER_WEB_SEARCH_GOOGLE_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    )
    cx = (
        _text(current.get("GOOGLE_SEARCH_CX") or current.get("google_search_cx") or current.get("GOOGLE_CX"))
        or _text(legacy_cx)
        or _text(legacy_hash.get("GOOGLE_CX") or legacy_hash.get("google_cx"))
        or _text(os.getenv("TATER_WEB_SEARCH_GOOGLE_CX") or os.getenv("GOOGLE_CX"))
    )
    return {"GOOGLE_API_KEY": api_key, "GOOGLE_SEARCH_CX": cx}


def save_google_search_settings(*, api_key: Any = None, search_cx: Any = None) -> Dict[str, str]:
    current = read_google_search_settings()
    next_settings = {
        "GOOGLE_API_KEY": _text(current.get("GOOGLE_API_KEY") if api_key is None else api_key),
        "GOOGLE_SEARCH_CX": _text(current.get("GOOGLE_SEARCH_CX") if search_cx is None else search_cx),
    }
    redis_client.hset(GOOGLE_SEARCH_SETTINGS_KEY, mapping=next_settings)
    return read_google_search_settings()


def read_integration_settings() -> Dict[str, Any]:
    settings = read_google_search_settings()
    return {
        "google_api_key": settings.get("GOOGLE_API_KEY", ""),
        "google_search_cx": settings.get("GOOGLE_SEARCH_CX", ""),
    }


def save_integration_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    saved = save_google_search_settings(
        api_key=(payload or {}).get("google_api_key"),
        search_cx=(payload or {}).get("google_search_cx"),
    )
    return {
        "google_api_key": saved.get("GOOGLE_API_KEY", ""),
        "google_search_cx": saved.get("GOOGLE_SEARCH_CX", ""),
    }


def integration_status() -> Dict[str, Any]:
    settings = read_google_search_settings()
    configured = bool(settings.get("GOOGLE_API_KEY") and settings.get("GOOGLE_SEARCH_CX"))
    return {
        "configured": configured,
        "message": "Google Custom Search is configured." if configured else "Google API key and Search CX are required.",
    }


def _truncate_snippet(value: Any) -> str:
    snippet = _text(value)
    if len(snippet) > GOOGLE_SEARCH_MAX_SNIPPET_CHARS:
        snippet = snippet[:GOOGLE_SEARCH_MAX_SNIPPET_CHARS].rstrip() + "..."
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
    search_time: Any = None,
    total_results: Any = None,
    next_start: Any = None,
) -> Dict[str, Any]:
    try:
        next_start_int = int(next_start) if next_start is not None else None
    except Exception:
        next_start_int = None
    try:
        total_results_int = int(total_results) if total_results is not None else None
    except Exception:
        total_results_int = None
    return {
        "tool": "search_web",
        "ok": True,
        "provider": "google_search",
        "provider_label": "Google Custom Search",
        "query": query,
        "start": start_index,
        "count": len(results),
        "num_results": max_results,
        "results": results,
        "site_filter": site_val or None,
        "search_time_sec": search_time,
        "total_results": total_results_int,
        "has_more": bool(next_start_int),
        "next_start": next_start_int,
    }


def _error(message: str, *, needs: Optional[List[str]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "tool": "search_web",
        "ok": False,
        "provider": "google_search",
        "provider_label": "Google Custom Search",
        "error": message,
    }
    if needs:
        out["needs"] = needs
    return out


def _read_json(req: urllib.request.Request, *, timeout_val: int) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    try:
        with urllib.request.urlopen(req, timeout=timeout_val) as resp:
            raw = resp.read(GOOGLE_SEARCH_MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(1000).decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        return None, _error(f"Google Custom Search request failed ({exc.code}): {body or exc}")
    except Exception as exc:
        return None, _error(f"Google Custom Search failed: {exc}")
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None, _error("Invalid response from Google Custom Search.")
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
    timeout_sec: Any = GOOGLE_SEARCH_DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    q = _text(query)
    if not q:
        return _error("query is required.")
    settings = read_google_search_settings()
    if not settings.get("GOOGLE_API_KEY") or not settings.get("GOOGLE_SEARCH_CX"):
        return _error(
            "Google Custom Search is not configured.",
            needs=[
                "Set Google API Key in the Google Custom Search integration settings.",
                "Set Google Search CX in the Google Custom Search integration settings.",
            ],
        )

    max_results = _bounded_int(num_results, default=5, minimum=1, maximum=10)
    start_index = _bounded_int(start, default=1, minimum=1, maximum=91)
    timeout_val = _bounded_int(timeout_sec, default=GOOGLE_SEARCH_DEFAULT_TIMEOUT_SECONDS, minimum=3, maximum=60)
    safe_mode = _text(safe).lower()
    if safe_mode not in {"active", "off"}:
        safe_mode = "active"
    site_val = _text(site)
    country_val = _text(country).lower()
    language_val = _text(language).lower()
    if language_val.startswith("lang_"):
        language_val = language_val[5:]

    params: Dict[str, Any] = {
        "key": settings.get("GOOGLE_API_KEY"),
        "cx": settings.get("GOOGLE_SEARCH_CX"),
        "q": q,
        "num": max_results,
        "start": start_index,
        "safe": safe_mode,
    }
    if site_val:
        params["siteSearch"] = site_val
        params["siteSearchFilter"] = "i"
    if country_val and re.fullmatch(r"[a-z]{2}", country_val):
        params["gl"] = country_val
    if language_val and re.fullmatch(r"[a-z]{2}", language_val):
        params["lr"] = f"lang_{language_val}"

    req = urllib.request.Request(
        GOOGLE_SEARCH_API_URL + "?" + urllib.parse.urlencode(params, doseq=True),
        headers={"User-Agent": "Tater-AgentLab/1.0"},
    )
    payload, error = _read_json(req, timeout_val=timeout_val)
    if error:
        return error
    if payload.get("error"):
        err = payload.get("error") or {}
        return _error(_text(err.get("message")) or "Unknown Google Custom Search error.")

    results: List[Dict[str, Any]] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        link = _text(item.get("link"))
        if not link:
            continue
        results.append(
            {
                "title": _text(item.get("title")) or link,
                "url": link,
                "snippet": _truncate_snippet(item.get("snippet")),
                "display_url": _text(item.get("displayLink")) or _display_url(link),
            }
        )

    search_info = payload.get("searchInformation") if isinstance(payload.get("searchInformation"), dict) else {}
    total_results = None
    next_start = None
    queries_blob = payload.get("queries") if isinstance(payload.get("queries"), dict) else {}
    req_pages = queries_blob.get("request") if isinstance(queries_blob.get("request"), list) else []
    if req_pages:
        total_results = req_pages[0].get("totalResults") if isinstance(req_pages[0], dict) else None
    next_pages = queries_blob.get("nextPage") if isinstance(queries_blob.get("nextPage"), list) else []
    if next_pages:
        next_start = next_pages[0].get("startIndex") if isinstance(next_pages[0], dict) else None
    return _success(
        query=q,
        start_index=start_index,
        max_results=max_results,
        results=results,
        site_val=site_val,
        search_time=search_info.get("searchTime") if isinstance(search_info, dict) else None,
        total_results=total_results,
        next_start=next_start,
    )


def run_integration_action(action_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if _text(action_id) != "test":
        raise KeyError(f"Unsupported Google Custom Search action: {action_id}")
    query = _text((payload or {}).get("query")) or "tater search test"
    result = integration_web_search(query, num_results=1, timeout_sec=GOOGLE_SEARCH_DEFAULT_TIMEOUT_SECONDS)
    if not result.get("ok"):
        raise RuntimeError(_text(result.get("error")) or "Google Custom Search test failed.")
    return {"ok": True, "message": "Google Custom Search returned a result.", "count": result.get("count", 0)}
