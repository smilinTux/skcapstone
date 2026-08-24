"""Encrypted, server-side browser sessions for the read-only runtime."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

COOKIE_NAME = "__Host-skdashboard_session"
SCOPES = "openid skdashboard.read skdashboard.events.read"
SESSION_TTL = 8 * 60 * 60
LOGIN_TTL = 5 * 60


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _opaque() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class SessionConfig:
    issuer: str
    redirect_uri: str
    client_secret: str

    def __post_init__(self) -> None:
        if not self.issuer.startswith("https://") or self.issuer.endswith("/"):
            raise ValueError("issuer must be an exact HTTPS origin")
        if not self.redirect_uri.startswith("https://") or not self.client_secret:
            raise ValueError("redirect URI and confidential client secret are required")


class OIDCClient:
    def __init__(self, config: SessionConfig) -> None:
        self.config = config

    async def exchange(self, values: dict[str, str], *, expected_nonce: str | None = None) -> dict:
        values = {**values, "client_id": "skdashboard", "client_secret": self.config.client_secret}
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(f"{self.config.issuer}/oidc/token", data=values)
            response.raise_for_status()
            result = response.json()
            if expected_nonce is not None:
                discovery = await client.get(
                    f"{self.config.issuer}/.well-known/openid-configuration"
                )
                discovery.raise_for_status()
                metadata = discovery.json()
                if metadata.get("issuer") != self.config.issuer:
                    raise ValueError("issuer mismatch")
                keys_response = await client.get(metadata["jwks_uri"])
                keys_response.raise_for_status()
                header = jwt.get_unverified_header(result["id_token"])
                matches = [
                    key
                    for key in keys_response.json()["keys"]
                    if key.get("kid") == header.get("kid")
                ]
                if len(matches) != 1:
                    raise ValueError("signing key unavailable")
                claims = jwt.decode(
                    result["id_token"],
                    jwt.PyJWK.from_dict(matches[0]).key,
                    algorithms=["RS256"],
                    audience="skdashboard",
                    issuer=self.config.issuer,
                    options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]},
                )
                if not secrets.compare_digest(str(claims["nonce"]), expected_nonce):
                    raise ValueError("nonce mismatch")
        return result


class EncryptedSessionAdapter:
    """Own opaque cookie handles while CapAuth credentials remain encrypted at rest."""

    def __init__(
        self,
        database: Path,
        key_file: Path,
        config: SessionConfig,
        *,
        oidc_client=None,
        clock=time.time,
    ) -> None:
        self.database = database
        self.config = config
        self.oidc = oidc_client or OIDCClient(config)
        self.clock = clock
        key = key_file.read_bytes().strip()
        if key_file.stat().st_mode & 0o077:
            raise PermissionError("session key must be mode 0600")
        self.cipher = Fernet(key)
        database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(database.parent, 0o700)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS login_transactions (
                    state_hash TEXT PRIMARY KEY, encrypted BLOB NOT NULL, expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    handle_hash TEXT PRIMARY KEY, encrypted BLOB NOT NULL, expires_at INTEGER NOT NULL
                );
                """
            )
        os.chmod(self.database, 0o600)

    def backup(self, destination: Path) -> None:
        """Create one consistent encrypted SQLite backup with private permissions."""
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(destination.parent, 0o700)
        with self._connect() as source, sqlite3.connect(destination) as target:
            source.backup(target)
        os.chmod(destination, 0o600)

    def _seal(self, value: dict) -> bytes:
        return self.cipher.encrypt(json.dumps(value, sort_keys=True).encode())

    def _open(self, value: bytes) -> dict:
        return json.loads(self.cipher.decrypt(value))

    def routes(self) -> list[Route]:
        return [
            Route("/auth/login", self.login),
            Route("/auth/callback", self.callback),
            Route("/auth/session", self.session),
            Route("/auth/logout", self.logout, methods=["POST"]),
        ]

    async def login(self, _request: Request) -> Response:
        state, nonce, verifier = _opaque(), _opaque(), _opaque() + _opaque()
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        payload = {"nonce": nonce, "verifier": verifier}
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO login_transactions VALUES (?, ?, ?)",
                (_digest(state), self._seal(payload), int(self.clock()) + LOGIN_TTL),
            )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": "skdashboard",
                "redirect_uri": self.config.redirect_uri,
                "scope": SCOPES,
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "issuer": self.config.issuer,
            }
        )
        return RedirectResponse(f"{self.config.issuer}/oidc/authorize?{query}", status_code=302)

    async def callback(self, request: Request) -> Response:
        state, code = request.query_params.get("state", ""), request.query_params.get("code", "")
        if not state or not code:
            return JSONResponse({"error": "invalid_callback"}, status_code=400)
        now = int(self.clock())
        with self._connect() as connection:
            row = connection.execute(
                "DELETE FROM login_transactions WHERE state_hash = ? RETURNING encrypted, expires_at",
                (_digest(state),),
            ).fetchone()
        if row is None or row["expires_at"] <= now:
            return JSONResponse({"error": "invalid_callback"}, status_code=400)
        try:
            transaction = self._open(row["encrypted"])
            token = await self.oidc.exchange(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.config.redirect_uri,
                    "code_verifier": transaction["verifier"],
                },
                expected_nonce=transaction["nonce"],
            )
            record = self._validate_token_response(token, now)
        except Exception:
            return JSONResponse({"error": "authentication_unavailable"}, status_code=503)
        handle, csrf = _opaque(), _opaque()
        record["csrf"] = csrf
        record["expires_at"] = now + SESSION_TTL
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                (_digest(handle), self._seal(record), record["expires_at"]),
            )
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            handle,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
            max_age=SESSION_TTL,
        )
        return response

    def _validate_token_response(self, token: dict, now: int) -> dict:
        required = {"access_token", "refresh_token", "expires_in", "scope", "token_type"}
        if not required.issubset(token) or token["token_type"] != "Bearer":
            raise ValueError("invalid token response")
        if type(token["expires_in"]) is not int or not 1 <= token["expires_in"] <= 300:
            raise ValueError("invalid token lifetime")
        if frozenset(str(token["scope"]).split()) != frozenset(
            {"skdashboard.read", "skdashboard.events.read"}
        ):
            raise ValueError("invalid token scope")
        return {
            "access_token": str(token["access_token"]),
            "refresh_token": str(token["refresh_token"]),
            "access_expires_at": now + token["expires_in"],
        }

    def _load(self, request: Request) -> tuple[str, dict] | None:
        handle = request.cookies.get(COOKIE_NAME, "")
        if not handle:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT encrypted, expires_at FROM sessions WHERE handle_hash = ?",
                (_digest(handle),),
            ).fetchone()
        if row is None or row["expires_at"] <= int(self.clock()):
            return None
        try:
            return handle, self._open(row["encrypted"])
        except (InvalidToken, ValueError, TypeError, json.JSONDecodeError):
            return None

    async def resolve(self, request: Request) -> str | None:
        loaded = self._load(request)
        if loaded is None:
            return None
        handle, record = loaded
        now = int(self.clock())
        if record["access_expires_at"] > now + 15:
            return record["access_token"]
        old_encrypted = self._seal(record)
        with self._connect() as connection:
            current = connection.execute(
                "SELECT encrypted FROM sessions WHERE handle_hash = ?", (_digest(handle),)
            ).fetchone()
            if current is None:
                return None
            old_encrypted = current["encrypted"]
            reserved = dict(record)
            reserved["refreshing"] = True
            reserved_encrypted = self._seal(reserved)
            changed = connection.execute(
                "UPDATE sessions SET encrypted = ? WHERE handle_hash = ? AND encrypted = ?",
                (reserved_encrypted, _digest(handle), old_encrypted),
            ).rowcount
        if changed != 1:
            return None
        try:
            token = await self.oidc.exchange(
                {"grant_type": "refresh_token", "refresh_token": record["refresh_token"]}
            )
            fresh = self._validate_token_response(token, now)
        except Exception:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE sessions SET encrypted = ? WHERE handle_hash = ? AND encrypted = ?",
                    (old_encrypted, _digest(handle), reserved_encrypted),
                )
            return None
        record.update(fresh)
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE sessions SET encrypted = ? WHERE handle_hash = ? AND encrypted = ?",
                (self._seal(record), _digest(handle), reserved_encrypted),
            ).rowcount
        return record["access_token"] if changed == 1 else None

    async def session(self, request: Request) -> Response:
        loaded = self._load(request)
        if loaded is None:
            return JSONResponse(
                {"authenticated": False}, status_code=401, headers={"Cache-Control": "no-store"}
            )
        return JSONResponse(
            {"authenticated": True, "csrf_token": loaded[1]["csrf"]},
            headers={"Cache-Control": "no-store"},
        )

    async def logout(self, request: Request) -> Response:
        origin = request.headers.get("origin", "")
        loaded = self._load(request)
        if (
            origin not in {self.config.redirect_uri.rsplit("/auth/callback", 1)[0]}
            or loaded is None
        ):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        handle, record = loaded
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), record["csrf"]):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE handle_hash = ?", (_digest(handle),))
        response = Response(status_code=204)
        response.delete_cookie(
            COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict"
        )
        return response


__all__ = ["COOKIE_NAME", "EncryptedSessionAdapter", "SessionConfig"]
