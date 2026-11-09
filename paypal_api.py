"""PayPal Sandbox API client and encrypted credential storage."""
import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request

from cryptography.fernet import Fernet, InvalidToken


class PayPalError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def configured(encryption_secret, return_base_url):
    if (
        not encryption_secret
        or encryption_secret in (
            "dev-only-change-me",
            "change-me-to-a-long-random-string",
            "replace-with-a-random-secret-at-least-32-characters-long",
        )
        or len(encryption_secret) < 32
    ):
        return False
    try:
        callback = urllib.parse.urlsplit(return_base_url.strip())
    except ValueError:
        return False
    local_http = callback.scheme == "http" and callback.hostname in ("localhost", "127.0.0.1", "::1")
    secure_https = callback.scheme == "https" and bool(callback.hostname)
    return (
        (local_http or secure_https)
        and not callback.username
        and not callback.password
        and not callback.query
        and not callback.fragment
    )


def _fernet(encryption_secret):
    key = hmac.new(
        encryption_secret.encode("utf-8"),
        b"paypilot-paypal-credentials-v1",
        hashlib.sha256,
    ).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_credentials(client_id, client_secret, encryption_secret):
    cipher = _fernet(encryption_secret)
    return (
        cipher.encrypt(client_id.encode("utf-8")).decode("ascii"),
        cipher.encrypt(client_secret.encode("utf-8")).decode("ascii"),
    )


def decrypt_credentials(encrypted_client_id, encrypted_client_secret, encryption_secret):
    cipher = _fernet(encryption_secret)
    try:
        return (
            cipher.decrypt(encrypted_client_id.encode("ascii")).decode("utf-8"),
            cipher.decrypt(encrypted_client_secret.encode("ascii")).decode("utf-8"),
        )
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise PayPalError("Saved PayPal credentials could not be decrypted. Re-enter them.") from exc


def _base_url():
    # This project intentionally supports Sandbox only.
    return "https://api-m.sandbox.paypal.com"


def _read_json(response):
    try:
        return json.loads(response.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PayPalError("PayPal returned an unreadable response") from exc


def _access_token(client_id, secret):
    client_id = client_id.strip()
    secret = secret.strip()
    if not client_id or not secret:
        raise PayPalError("PayPal Sandbox credentials are not configured")
    auth = base64.b64encode(f"{client_id}:{secret}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        f"{_base_url()}/v1/oauth2/token",
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("ascii"),
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            data = _read_json(response)
    except urllib.error.HTTPError as exc:
        raise PayPalError(f"PayPal Sandbox credentials were rejected (HTTP {exc.code})", status=400) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PayPalError("Could not reach PayPal Sandbox") from exc
    token = data.get("access_token") if isinstance(data, dict) else None
    if not token:
        raise PayPalError("PayPal did not return an access token")
    return token


def validate_credentials(client_id, client_secret):
    _access_token(client_id, client_secret)


def request(method, path, payload, client_id, client_secret, request_id=None):
    headers = {
        "Authorization": f"Bearer {_access_token(client_id, client_secret)}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    if request_id:
        headers["PayPal-Request-Id"] = request_id
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{_base_url()}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            return _read_json(response)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            error = json.loads(exc.read().decode("utf-8"))
            detail = error.get("message") or (
                error.get("details", [{}])[0].get("issue", "") if error.get("details") else ""
            )
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, IndexError, TypeError):
            pass
        message = f"PayPal returned HTTP {exc.code}"
        if detail:
            message += f": {detail[:180]}"
        raise PayPalError(message, status=502) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PayPalError("Could not reach PayPal Sandbox", status=502) from exc
