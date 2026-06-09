"""Shadow for outbound HTTP requests.

Does not perform the request; returns the parsed structure so a human can
review what would be sent (method, URL, headers, body summary).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


async def http_shadow(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: str | bytes | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    parsed = urlparse(url)
    body_size = len(body) if isinstance(body, bytes) else len(body.encode()) if body else 0

    safe_method = method.upper()
    destructive = safe_method in {"DELETE", "PUT", "PATCH"}

    redacted_headers: dict[str, str] = {}
    for k, v in (headers or {}).items():
        if k.lower() in {"authorization", "x-api-key", "cookie", "set-cookie"}:
            redacted_headers[k] = "<redacted>"
        else:
            redacted_headers[k] = v

    return {
        "method": safe_method,
        "url": url,
        "host": parsed.netloc,
        "path": parsed.path,
        "scheme": parsed.scheme,
        "headers": redacted_headers,
        "body_bytes": body_size,
        "destructive": destructive,
        "timeout_seconds": timeout,
        "note": "no real execution — http_shadow preview only",
    }
