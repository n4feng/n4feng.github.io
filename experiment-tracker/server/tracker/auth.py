"""Root-code authentication with expiring signed sessions."""

import hashlib
import hmac
import math
import time
from functools import wraps
from threading import Lock

from flask import current_app, g, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.exceptions import TooManyRequests, Unauthorized



def serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="experiment-tracker-session-v1")


def root_fingerprint():
    # Changing the access code also invalidates existing sessions.
    return hmac.new(
        current_app.config["SECRET_KEY"].encode(),
        current_app.config["ROOT_ACCESS_CODE"].encode(),
        hashlib.sha256,
    ).hexdigest()


def issue_token():
    return serializer().dumps({"role": "root", "version": root_fingerprint()})


class LocalFailureLimiter:
    """Bounded best-effort defense within one process/Lambda environment.

    Authentication never reads or writes experiment storage. Independent Lambda
    environments have separate budgets; this is not a distributed rate limiter.
    """

    def __init__(self):
        self.failures = {}
        self.lock = Lock()

    def check(self, client, valid, window, maximum):
        now = time.time()
        with self.lock:
            attempts = [stamp for stamp in self.failures.get(client, []) if stamp > now - window]
            if len(attempts) >= maximum:
                raise TooManyRequests(
                    "Too many failed access-code attempts. Try again later.",
                    retry_after=max(1, math.ceil(attempts[0] + window - now)),
                )
            if valid:
                self.failures.pop(client, None)
            else:
                if len(self.failures) >= 4096:
                    self.failures = {key: values for key, values in self.failures.items() if values[-1] > now - window}
                    if len(self.failures) >= 4096 and client not in self.failures:
                        self.failures.pop(next(iter(self.failures)))
                self.failures[client] = [*attempts, now]


def check_access_code(code):
    valid = isinstance(code, str) and hmac.compare_digest(code.encode(), current_app.config["ROOT_ACCESS_CODE"].encode())
    current_app.extensions["tracker_auth_limiter"].check(
        request.remote_addr or "unknown", valid,
        current_app.config["AUTH_RATE_WINDOW_SECONDS"], current_app.config["AUTH_RATE_MAX_FAILURES"],
    )
    if not valid:
        raise Unauthorized("Invalid access code.")


def require_auth(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        authorization = request.headers.get("Authorization", "")
        if authorization:
            kind, separator, token = authorization.partition(" ")
            if kind.lower() != "bearer" or not separator or not token:
                raise Unauthorized("Use Authorization: Bearer <session token>.")
            try:
                payload = serializer().loads(token, max_age=current_app.config["SESSION_TTL_SECONDS"])
                if not isinstance(payload, dict) or payload.get("role") != "root" or not hmac.compare_digest(
                    str(payload.get("version", "")), root_fingerprint()
                ):
                    raise BadSignature("Invalid session.")
            except SignatureExpired:
                raise Unauthorized("Session expired. Enter the access code again.") from None
            except BadSignature:
                raise Unauthorized("Invalid session. Enter the access code again.") from None
        elif "X-Access-Code" in request.headers:
            check_access_code(request.headers["X-Access-Code"])
        else:
            raise Unauthorized("An access code or signed session is required.")
        g.role = "root"
        return handler(*args, **kwargs)

    return wrapped
