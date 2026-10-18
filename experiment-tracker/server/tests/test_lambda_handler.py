"""HTTP protocol checks for AWS Function URL requests, without an AWS account."""

import base64
import gzip
import json
import unittest
from unittest.mock import patch

from flask import Flask, Response, request

import lambda_handler


def event(method="GET", path="/", body=None, headers=None, **overrides):
    return {
        "version": "2.0",
        "rawPath": path,
        "rawQueryString": "",
        "requestContext": {
            "domainName": "test.lambda-url.us-east-1.on.aws",
            "http": {"method": method, "path": path, "sourceIp": "203.0.113.21", "protocol": "HTTP/1.1"},
        },
        "headers": {"host": "test.lambda-url.us-east-1.on.aws", **(headers or {})},
        "body": body,
        "isBase64Encoded": False,
        **overrides,
    }


class LambdaAdapterTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

        @self.app.route("/echo/<path:name>", methods=["GET", "POST", "PATCH"])
        def echo(name):
            return {
                "name": name,
                "method": request.method,
                "remote_addr": request.remote_addr,
                "authorization": request.headers.get("Authorization"),
                "content_type": request.content_type,
                "content_length": request.content_length,
                "body_hex": request.get_data().hex(),
                "query_tags": request.args.getlist("tag"),
                "query_q": request.args.get("q"),
                "cookies": dict(request.cookies),
                "scheme": request.scheme,
            }

        @self.app.get("/binary")
        def binary():
            response = Response(b"\x00\xff\xfe\x80", content_type="application/octet-stream")
            response.set_cookie("first", "one", secure=True, httponly=True)
            response.set_cookie("second", "two", secure=True, httponly=True)
            response.headers.add("X-Example", "first")
            response.headers.add("X-Example", "second")
            return response

    def test_utf8_path_repeated_queries_binary_body_cookies_and_trusted_source(self):
        raw = b"\x00\xffupload"
        incoming = event(
            "PATCH", "/echo/caf%C3%A9",
            body=base64.b64encode(raw).decode(),
            headers={"Content-Type": "application/octet-stream", "Content-Length": "9999", "Authorization": "Bearer signed-token", "X-Forwarded-For": "198.51.100.9"},
            isBase64Encoded=True,
            rawQueryString="tag=first&tag=second&q=a%2Bb+space",
            cookies=["session=one", "theme=dark"],
        )
        response = lambda_handler.wsgi_response(self.app, incoming)
        self.assertEqual(response["statusCode"], 200)
        self.assertFalse(response["isBase64Encoded"])
        data = json.loads(response["body"])
        self.assertEqual(data["name"], "café")
        self.assertEqual(data["method"], "PATCH")
        self.assertEqual(data["remote_addr"], "203.0.113.21")
        self.assertEqual(data["authorization"], "Bearer signed-token")
        self.assertEqual(data["content_type"], "application/octet-stream")
        self.assertEqual(data["content_length"], len(raw))
        self.assertEqual(data["body_hex"], raw.hex())
        self.assertEqual(data["query_tags"], ["first", "second"])
        self.assertEqual(data["query_q"], "a+b space")
        self.assertEqual(data["cookies"], {"session": "one", "theme": "dark"})
        self.assertEqual(data["scheme"], "https")

    def test_plain_utf8_json_body_is_not_treated_as_base64(self):
        body = json.dumps({"name": "海洋"}, ensure_ascii=False)
        response = lambda_handler.wsgi_response(self.app, event("POST", "/echo/plain", body, {"content-type": "application/json"}))
        data = json.loads(response["body"])
        self.assertEqual(bytes.fromhex(data["body_hex"]).decode(), body)
        self.assertEqual(data["content_length"], len(body.encode()))

    def test_binary_response_and_repeated_cookies_survive_translation(self):
        response = lambda_handler.wsgi_response(self.app, event(path="/binary"))
        self.assertEqual(response["statusCode"], 200)
        self.assertTrue(response["isBase64Encoded"])
        self.assertEqual(base64.b64decode(response["body"]), b"\x00\xff\xfe\x80")
        self.assertEqual(response["headers"]["content-type"], "application/octet-stream")
        self.assertEqual(response["headers"]["x-example"], "first, second")
        self.assertEqual(len(response["cookies"]), 2)
        self.assertNotIn("set-cookie", response["headers"])

    def test_head_response_keeps_headers_and_suppresses_body(self):
        response = lambda_handler.wsgi_response(self.app, event("HEAD", "/binary"))
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(response["body"], "")
        self.assertFalse(response["isBase64Encoded"])
        self.assertEqual(response["headers"]["content-length"], "4")

    def test_generic_wsgi_iterable_is_closed_and_write_callable_is_supported(self):
        class Result:
            closed = False

            def __iter__(self):
                yield b"second"

            def close(self):
                self.closed = True

        result = Result()

        def application(environ, start_response):
            write = start_response("200 OK", [("Content-Type", "text/plain"), ("Connection", "keep-alive"), ("Transfer-Encoding", "chunked")])
            write(b"first-")
            return result

        response = lambda_handler.wsgi_response(application, event())
        self.assertTrue(result.closed)
        self.assertEqual(response["body"], "first-second")
        self.assertNotIn("connection", response["headers"])
        self.assertNotIn("transfer-encoding", response["headers"])

    def test_handler_creates_application_once_and_reuses_it(self):
        with patch.object(lambda_handler, "_application", None), patch.object(lambda_handler, "create_app", return_value=self.app) as factory:
            for _ in range(2):
                response = lambda_handler.handler(event(path="/echo/warm"), None)
                self.assertEqual(response["statusCode"], 200)
            factory.assert_called_once_with()

    def test_unsupported_payload_format_is_rejected_explicitly(self):
        with self.assertRaisesRegex(ValueError, "2.0"):
            lambda_handler.wsgi_response(self.app, {"version": "1.0"})

    def test_negotiated_gzip_preserves_unicode_and_fits_buffered_budget(self):
        body = '海洋 "experiment"\n' * 1000

        @self.app.get("/large")
        def large():
            return Response(body, content_type="text/plain; charset=utf-8", headers={"Vary": "Origin"})

        with patch.object(lambda_handler, "MAX_BUFFERED_RESPONSE_BYTES", 1024):
            response = lambda_handler.wsgi_response(self.app, event(path="/large", headers={"accept-encoding": "gzip, br"}))
        self.assertEqual(response["statusCode"], 200)
        self.assertTrue(response["isBase64Encoded"])
        self.assertEqual(response["headers"]["content-encoding"], "gzip")
        compressed = base64.b64decode(response["body"])
        self.assertEqual(gzip.decompress(compressed).decode(), body)
        self.assertEqual(response["headers"]["content-length"], str(len(compressed)))
        self.assertIn("Origin", response["headers"]["vary"])
        self.assertIn("Accept-Encoding", response["headers"]["vary"])

    def test_explicit_gzip_refusal_overrides_wildcard(self):
        body = "Compressible response " * 100

        @self.app.get("/large")
        def large():
            return Response(body, content_type="text/plain")

        for value in ("gzip;q=0", "gzip;q=0, *;q=1", "br", "gzip;q=invalid"):
            with self.subTest(accept_encoding=value):
                response = lambda_handler.wsgi_response(self.app, event(path="/large", headers={"accept-encoding": value}))
                self.assertEqual(response["body"], body)
                self.assertNotIn("content-encoding", response["headers"])

    def test_response_limit_counts_json_escaping_and_returns_cors_readable_error(self):
        # The raw body is under the budget; JSON envelope escaping exceeds it.
        body = '"' * 700

        @self.app.get("/quotes")
        def quotes():
            return Response(body, headers={"Access-Control-Allow-Origin": "https://n4feng.github.io", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

        with patch.object(lambda_handler, "MAX_BUFFERED_RESPONSE_BYTES", 1024):
            response = lambda_handler.wsgi_response(self.app, event(path="/quotes"))
        self.assertEqual(response["statusCode"], 413)
        self.assertIn("limit=1", json.loads(response["body"])["error"])
        self.assertEqual(response["headers"]["access-control-allow-origin"], "https://n4feng.github.io")
        self.assertEqual(response["headers"]["cache-control"], "no-store")
        self.assertEqual(response["headers"]["x-content-type-options"], "nosniff")
        self.assertEqual(response["headers"]["content-type"], "application/json")
        self.assertEqual(int(response["headers"]["content-length"]), len(response["body"].encode()))
        self.assertNotIn("content-encoding", response["headers"])
        self.assertFalse(response["isBase64Encoded"])


if __name__ == "__main__":
    unittest.main()
