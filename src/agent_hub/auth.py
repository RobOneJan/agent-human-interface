"""Auth for calling an MCP server hosted behind Cloud Run IAM.

Mirrors the pattern already established for email-mcp-server: no custom
token-verification code, no static secret to leak - fetch a short-lived,
Google-signed identity token for this process's own credentials (its
attached service account when running on GCP, or local `gcloud`
Application Default Credentials during development) and send it as a
Bearer token. Cloud Run's own IAM layer does the verification.
"""

from __future__ import annotations

from urllib.parse import urlparse


def is_local(url: str) -> bool:
    return urlparse(url).hostname in ("localhost", "127.0.0.1")


def fetch_auth_header(url: str) -> dict[str, str]:
    """Return the headers needed to call `url`.

    Empty for a local URL (e.g. `http://localhost:8080` via
    `gcloud run services proxy`, which already injects auth) - avoids
    requiring Application Default Credentials just to run this locally
    against a proxied dev server.
    """
    if is_local(url):
        return {}
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    parsed = urlparse(url)
    audience = f"{parsed.scheme}://{parsed.netloc}"  # Cloud Run checks the service origin, not the path
    token = id_token.fetch_id_token(Request(), audience)
    return {"Authorization": f"Bearer {token}"}
