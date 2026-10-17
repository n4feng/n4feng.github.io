"""Root-code authentication with expiring signed sessions."""

import hashlib
import hmac
import math
import time
from functools import wraps

from flask import current_app, g, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.exceptions import TooManyRequests, Unauthorized

from .db import get_db


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


def check_access_code(code):
    """Rate-limit failed code attempts across workers using shared SQLite."""
    now = time.time()
    window = current_app.config["AUTH_RATE_WINDOW_SECONDS"]
    maximum = current_app.config["AUTH_RATE_MAX_FAILURES"]
    client = request.remote_addr or "unknown"
    db = get_db()
    # Serialize attempts so parallel requests cannot bypass the failure cap.
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("DELETE FROM auth_failures WHERE occurred_at <= ?", (now - window,))
        failures = db.execute(
            "SELECT COUNT(*), MIN(occurred_at) FROM auth_failures WHERE client = ?", (client,)
        ).fetchone()
        if failures[0] >= maximum:
            retry_after = max(1, math.ceil(failures[1] + window - now))
            db.commit()
            raise TooManyRequests("Too many failed access-code attempts. Try again later.", retry_after=retry_after)
        valid = isinstance(code, str) and hmac.compare_digest(code.encode(), current_app.config["ROOT_ACCESS_CODE"].encode())
        if valid:
            db.execute("DELETE FROM auth_failures WHERE client = ?", (client,))
        else:
            db.execute("INSERT INTO auth_failures (client, occurred_at) VALUES (?, ?)", (client, now))
        db.commit()
    except Exception:
        db.rollback()
        raise
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
