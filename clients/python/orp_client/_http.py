"""HTTP transport: authentication, retries with jittered backoff, and typed errors.

Rules this module keeps:

* The token comes only from a constructor argument, a callable, or an environment variable the caller names. It is
  read when a request is made, sent only in the ``Authorization`` header, and never logged or put in an error.
* Request and response bodies are never logged (they hold memory content). Logs carry the verb, status, attempt
  and the server's request id only.
* A request is retried only when the caller marks it safe to repeat: reads, and writes carrying an idempotency key.
  Retries happen on 429, 502, 503 and 504 responses and on network errors, with full-jitter exponential backoff
  that honours ``Retry-After``.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import os
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from .errors import TransportError, UnauthorizedError, error_from_response

log = logging.getLogger("orp_client")

RETRY_STATUSES = frozenset({429, 502, 503, 504})
_NETWORK_ERRORS = (urllib.error.URLError, ConnectionError, TimeoutError, socket.timeout, http.client.HTTPException,
                   OSError)

USER_AGENT = "orp-client-python/0.1"


def _is_loopback(host: str) -> bool:
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class Transport:
    def __init__(self, url: str, *, token: Optional[str] = None, token_env: Optional[str] = None,
                 get_token: Optional[Callable[[], str]] = None, timeout: float = 30, retries: int = 3,
                 allow_insecure_http: bool = False, backoff_base: float = 0.2, backoff_cap: float = 5.0,
                 sleep: Callable[[float], None] = time.sleep, rng: Callable[[], float] = random.random) -> None:
        sources = [x for x in (token, token_env, get_token) if x is not None]
        if len(sources) > 1:
            raise ValueError("pass at most one of token, token_env and get_token")
        if token is not None and not isinstance(token, str):
            raise TypeError("token must be a string")
        if token_env is not None and (not isinstance(token_env, str) or not token_env):
            raise TypeError("token_env is the NAME of an environment variable")
        if get_token is not None and not callable(get_token):
            raise TypeError("get_token must be callable")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("url must be an http(s) URL such as https://memory.example.com")
        if parsed.query or parsed.fragment:
            raise ValueError("url must not carry a query or fragment")
        if parsed.scheme == "http" and sources and not _is_loopback(parsed.hostname) and not allow_insecure_http:
            raise ValueError("refusing to send a bearer token over plain http to a non-loopback host; "
                             "use https, or pass allow_insecure_http=True if you really mean it")
        if retries < 0:
            raise ValueError("retries must be >= 0")
        self.base = url.rstrip("/")
        self._token = token
        self._token_env = token_env
        self._get_token = get_token
        self.timeout = timeout
        self.retries = int(retries)
        self.allow_insecure_http = allow_insecure_http
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._sleep = sleep
        self._rng = rng
        self._opener = urllib.request.build_opener()  # honours the usual proxy environment variables

    # ------------------------------------------------------------------ credentials
    def clone_with_token(self, token: str) -> "Transport":
        t = Transport(self.base, token=token, timeout=self.timeout, retries=self.retries,
                      allow_insecure_http=self.allow_insecure_http, backoff_base=self.backoff_base,
                      backoff_cap=self.backoff_cap, sleep=self._sleep, rng=self._rng)
        return t

    def _bearer(self) -> Optional[str]:
        if self._token is not None:
            tok = self._token
        elif self._token_env is not None:
            tok = os.environ.get(self._token_env)
            if not tok:
                raise UnauthorizedError("unauthorized", f"environment variable {self._token_env} is not set")
        elif self._get_token is not None:
            tok = self._get_token()
            if not isinstance(tok, str) or not tok:
                raise UnauthorizedError("unauthorized", "get_token() returned no token")
        else:
            return None
        if any(c in tok for c in "\r\n\0"):
            raise ValueError("token contains a control character")
        return tok

    def __repr__(self) -> str:  # never shows the token
        src = "token" if self._token is not None else f"env:{self._token_env}" if self._token_env else \
            "callable" if self._get_token else "none"
        return f"Transport({self.base!r}, credentials={src})"

    # ------------------------------------------------------------------ requests
    def url(self, path: str, query: Optional[Mapping[str, Any]] = None) -> str:
        u = self.base + path
        if query:
            q = {k: v for k, v in query.items() if v is not None}
            if q:
                u += "?" + urllib.parse.urlencode(q)
        return u

    def _delay(self, attempt: int, retry_after: Optional[str]) -> float:
        d = self._rng() * min(self.backoff_cap, self.backoff_base * (2 ** attempt))
        if retry_after:
            try:
                d = max(d, min(float(retry_after), 30.0))
            except ValueError:
                pass
        return d

    def request(self, method: str, path: str, *, verb: str, body: Any = None, query: Optional[Mapping[str, Any]] = None,
                retry: bool = False, headers: Optional[Dict[str, str]] = None,
                raw_response: bool = False) -> Tuple[Any, Dict[str, str]]:
        """Send one request; returns ``(json_body, response_headers)`` or raises a typed ``OrpError``."""
        data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        url = self.url(path, query)
        attempt = 0
        while True:
            hdrs = {"accept": "application/json", "user-agent": USER_AGENT}
            if data is not None:
                hdrs["content-type"] = "application/json"
            if headers:
                hdrs.update(headers)
            tok = self._bearer()  # re-read every attempt: rotated tokens take effect at once
            if tok is not None:
                hdrs["authorization"] = "Bearer " + tok
            req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
            status: Optional[int] = None
            resp_headers: Dict[str, str] = {}
            payload = b""
            failure: Optional[BaseException] = None
            try:
                with self._opener.open(req, timeout=self.timeout) as r:
                    status = r.status
                    resp_headers = {k.lower(): v for k, v in r.headers.items()}
                    payload = r.read()
            except urllib.error.HTTPError as e:
                status = e.code
                resp_headers = {k.lower(): v for k, v in (e.headers or {}).items()}
                try:
                    payload = e.read()
                except Exception:  # noqa: BLE001 - a broken error body is still an error response
                    payload = b""
                finally:
                    e.close()
            except _NETWORK_ERRORS as e:
                failure = e
            rid = resp_headers.get("x-request-id")
            if failure is not None:
                if retry and attempt < self.retries:
                    d = self._delay(attempt, None)
                    log.info("orp %s: network error (%s), retry %d in %.2fs", verb, type(failure).__name__,
                             attempt + 1, d)
                    self._sleep(d)
                    attempt += 1
                    continue
                note = "" if retry else " (not retried: this request is not marked safe to repeat)"
                raise TransportError("transport", f"{verb}: {type(failure).__name__}{note}") from None
            assert status is not None
            if status in RETRY_STATUSES and retry and attempt < self.retries:
                d = self._delay(attempt, resp_headers.get("retry-after"))
                log.info("orp %s: HTTP %d, retry %d in %.2fs (request %s)", verb, status, attempt + 1, d, rid)
                self._sleep(d)
                attempt += 1
                continue
            parsed: Any = None
            if payload:
                ctype = resp_headers.get("content-type", "")
                try:
                    parsed = json.loads(payload.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    if 200 <= status < 300 and "json" in ctype:
                        raise TransportError("transport", f"{verb}: response is not valid JSON", status, {}, rid) \
                            from None
                    parsed = None
            if 200 <= status < 300:
                log.debug("orp %s: HTTP %d (request %s)", verb, status, rid)
                if raw_response:
                    return payload, resp_headers
                return parsed if parsed is not None else {}, resp_headers
            err = error_from_response(status, parsed, rid)
            log.debug("orp %s: HTTP %d %s (request %s)", verb, status, err.code, rid)
            raise err


__all__ = ["Transport", "RETRY_STATUSES"]
