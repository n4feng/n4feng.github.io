"""Run the existing Flask API behind an AWS Lambda Function URL (payload v2).

No HTTP server, API Gateway, Lambda layer, or filesystem database is needed.
The event's AWS-supplied source address is authoritative for auth throttling;
caller-controlled forwarding headers never replace it.
"""

import base64
import gzip
import io
import json
import sys
from urllib.parse import unquote_to_bytes

from tracker import create_app

_application = None
MAX_BUFFERED_RESPONSE_BYTES = 5_900_000


def accepts_gzip(value):
    """Honor an explicit gzip refusal even when a wildcard is accepted."""
    encodings = {}
    for entry in value.lower().split(","):
        name, *parameters = entry.strip().split(";")
        quality = 1.0
        for parameter in parameters:
            key, separator, setting = parameter.strip().partition("=")
            if key == "q" and separator:
                try:
                    quality = float(setting)
                except ValueError:
                    quality = 0.0
        encodings[name.strip()] = quality
    return 0 < encodings.get("gzip", encodings.get("*", 0)) <= 1


def wsgi_response(application, event, context=None):
    """Translate a buffered Function URL request/response to the WSGI protocol."""
    if event.get("version") != "2.0":
        raise ValueError("Only Lambda Function URL payload format 2.0 is supported.")
    request_context = event["requestContext"]
    http = request_context["http"]
    headers = {name.lower(): value for name, value in (event.get("headers") or {}).items()}
    body = event.get("body") or ""
    body = base64.b64decode(body, validate=True) if event.get("isBase64Encoded") else body.encode("utf-8")
    # WSGI represents decoded URL bytes in a latin-1 string, including UTF-8
    # sequences. Werkzeug subsequently interprets those bytes correctly.
    path = unquote_to_bytes(event.get("rawPath") or http.get("path") or "/").decode("latin-1")
    domain = request_context.get("domainName") or headers.get("host", "localhost")
    environ = {
        "REQUEST_METHOD": http["method"],
        "SCRIPT_NAME": "",
        "PATH_INFO": path,
        "QUERY_STRING": event.get("rawQueryString", ""),
        "SERVER_NAME": domain,
        "SERVER_PORT": "443",
        "SERVER_PROTOCOL": http.get("protocol", "HTTP/1.1"),
        "REMOTE_ADDR": http["sourceIp"],
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "https",
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        "lambda.context": context,
    }
    for name, value in headers.items():
        if name == "content-type":
            environ["CONTENT_TYPE"] = value
        elif name != "content-length":
            environ["HTTP_" + name.upper().replace("-", "_")] = value
    if event.get("cookies"):
        environ["HTTP_COOKIE"] = "; ".join(event["cookies"])

    response_status = None
    response_headers = []
    chunks = []

    def start_response(status, outgoing_headers, exc_info=None):
        nonlocal response_status, response_headers
        if exc_info and response_status is not None:
            raise exc_info[1].with_traceback(exc_info[2])
        response_status = int(status.split(" ", 1)[0])
        response_headers = outgoing_headers
        return chunks.append

    result = application(environ, start_response)
    try:
        for chunk in result:
            chunks.append(chunk)
    finally:
        if hasattr(result, "close"):
            result.close()
    if response_status is None:
        raise RuntimeError("WSGI application did not call start_response.")

    outgoing = {}
    cookies = []
    for name, value in response_headers:
        name = name.lower()
        if name == "set-cookie":
            cookies.append(value)
        elif name not in {"transfer-encoding", "connection"}:
            outgoing[name] = f"{outgoing[name]}, {value}" if name in outgoing else value
    response_body = b"".join(chunks)
    # Flask already suppresses HEAD bodies; also support generic WSGI apps.
    if http["method"] == "HEAD" or response_status in {204, 304}:
        response_body = b""
    if len(response_body) >= 1024 and "content-encoding" not in outgoing:
        outgoing["vary"] = f"{outgoing['vary']}, Accept-Encoding" if outgoing.get("vary") else "Accept-Encoding"
        if accepts_gzip(headers.get("accept-encoding", "")):
            compressed = gzip.compress(response_body, compresslevel=6, mtime=0)
            if len(compressed) < len(response_body):
                response_body = compressed
                outgoing["content-encoding"] = "gzip"
                outgoing["content-length"] = str(len(compressed))
    try:
        encoded_body = response_body.decode("utf-8")
        is_base64 = False
    except UnicodeDecodeError:
        encoded_body = base64.b64encode(response_body).decode("ascii")
        is_base64 = True
    response = {
        "statusCode": response_status,
        "headers": outgoing,
        "body": encoded_body,
        "isBase64Encoded": is_base64,
    }
    if cookies:
        response["cookies"] = cookies
    # The Lambda envelope includes JSON escaping and (for compressed/binary
    # bodies) base64 expansion. Bound that wire representation, not just bytes
    # returned by Flask, with margin below Lambda's 6 MB buffered limit.
    if len(json.dumps(response, ensure_ascii=True).encode("utf-8")) > MAX_BUFFERED_RESPONSE_BYTES:
        error_body = json.dumps({
            "error": "Response exceeds the maximum buffered response size. Request fewer plots with limit=1 and enable gzip.",
        })
        outgoing.pop("content-encoding", None)
        outgoing["content-type"] = "application/json"
        outgoing["content-length"] = str(len(error_body.encode("utf-8")))
        response = {"statusCode": 413, "headers": outgoing, "body": error_body, "isBase64Encoded": False}
    return response


def handler(event, context):
    """Reuse the Flask application across warm invocations."""
    global _application
    if _application is None:
        _application = create_app()
    return wsgi_response(_application, event, context)
