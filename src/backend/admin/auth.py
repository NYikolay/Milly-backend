from __future__ import annotations

import hashlib
import hmac
import secrets

from pydantic import SecretStr
from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request


class AdminAuth(AuthenticationBackend):
    """Password-based session authentication for the SQLAdmin panel."""

    def __init__(
        self,
        *,
        username: str,
        password: SecretStr,
        secret_key: SecretStr,
        session_max_age_seconds: int,
        https_only: bool,
    ) -> None:
        """
        Initialize the admin authentication backend.

        Purpose:
            Configure the admin session cookie and the credential
            fingerprint used to invalidate sessions after a password change.

        Args:
            username: Admin login name.
            password: Admin password from configuration.
            secret_key: Secret used to sign the session cookie and fingerprint.
            session_max_age_seconds: Session cookie lifetime in seconds.
            https_only: Whether the cookie must carry the Secure flag.

        Notes:
            - The raw password is kept in memory for constant-time comparison.
            - The fingerprint is an HMAC-SHA256 of ``username:password``.
        """
        super().__init__(
            secret_key=secret_key.get_secret_value(),
            session_cookie="milly_admin_session",
            max_age=session_max_age_seconds,
            path="/admin",
            same_site="lax",
            https_only=https_only,
        )
        self._username = username
        self._password = password.get_secret_value()
        self._fingerprint = hmac.new(
            secret_key.get_secret_value().encode("utf-8"),
            f"{username}:{self._password}".encode(),
            hashlib.sha256,
        ).hexdigest()

    async def login(self, request: Request) -> bool:
        """
        Validate submitted admin credentials and start a session.

        Purpose:
            Compare the login form against configured credentials without
            leaking which value was wrong.

        Args:
            request: Login request carrying a URL-encoded form.

        Returns:
            bool: True when the credentials match and the session is marked
                authenticated; False otherwise.

        Notes:
            - Both comparisons run to avoid short-circuiting on the username.
            - The session stores only the credential fingerprint.
        """
        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        username_ok = secrets.compare_digest(
            username.encode("utf-8"),
            self._username.encode("utf-8"),
        )
        password_ok = secrets.compare_digest(
            password.encode("utf-8"),
            self._password.encode("utf-8"),
        )
        if not (username_ok and password_ok):
            return False
        request.session.update({"authenticated": self._fingerprint})
        return True

    async def logout(self, request: Request) -> bool:
        """
        Clear the admin session.

        Purpose:
            Remove the authenticated marker from the signed session cookie.

        Args:
            request: Request whose session is cleared.

        Returns:
            bool: Always True so SQLAdmin redirects to the login page.
        """
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        """
        Validate the admin session fingerprint.

        Purpose:
            Reject missing, forged, or stale sessions, including sessions
            issued before the configured password was changed.

        Args:
            request: Request whose session is inspected.

        Returns:
            bool: True only when the stored fingerprint matches the current
                credentials.
        """
        fingerprint = request.session.get("authenticated")
        if not isinstance(fingerprint, str):
            return False
        return secrets.compare_digest(
            fingerprint.encode("utf-8"),
            self._fingerprint.encode("utf-8"),
        )
