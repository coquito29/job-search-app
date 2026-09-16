"""HTTP security and Chrome-extension access policy for the Flask app."""

import os

from flask import request


_EXTENSION_CORS_PATHS = frozenset({
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/status",
    "/api/profile/full",
})


def install_security(app):
    """Attach security headers and narrowly scoped extension CORS routes."""

    @app.after_request
    def extension_cors(response):
        origin = request.headers.get("Origin", "")
        if (origin.startswith("chrome-extension://")
                and request.path in _EXTENSION_CORS_PATHS):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if os.environ.get("RENDER"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=15552000")
        return response

    @app.route("/api/auth/login", methods=["OPTIONS"])
    @app.route("/api/auth/logout", methods=["OPTIONS"])
    @app.route("/api/auth/status", methods=["OPTIONS"])
    @app.route("/api/profile/full", methods=["OPTIONS"])
    def extension_preflight():
        return ("", 204)
