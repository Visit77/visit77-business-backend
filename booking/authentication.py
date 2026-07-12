from dataclasses import dataclass

from django.conf import settings
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.backends import TokenBackend
from rest_framework_simplejwt.exceptions import TokenError


@dataclass(frozen=True)
class CoreJWTUser:
    id: int
    email: str = ""
    is_staff: bool = False
    is_superuser: bool = False
    is_authenticated: bool = True


class CoreJWTAuthentication(BaseAuthentication):
    """Authenticate Visit77 Core users without copying them into Booking Engine."""

    keyword = b"Bearer"

    def authenticate(self, request):
        header = get_authorization_header(request).split()
        if not header:
            return None
        if len(header) != 2 or header[0].lower() != self.keyword.lower():
            raise AuthenticationFailed("Use Authorization: Bearer <Core access token>.")
        signing_key = settings.CORE_JWT_SIGNING_KEY
        verifying_key = settings.CORE_JWT_VERIFYING_KEY
        if not signing_key and not verifying_key:
            raise AuthenticationFailed("Core JWT verification is not configured.")
        try:
            token = header[1].decode("utf-8")
            backend = TokenBackend(
                algorithm=settings.CORE_JWT_ALGORITHM,
                signing_key=signing_key,
                verifying_key=verifying_key,
                audience=settings.CORE_JWT_AUDIENCE or None,
                issuer=settings.CORE_JWT_ISSUER or None,
            )
            claims = backend.decode(token, verify=True)
        except (UnicodeDecodeError, TokenError) as exc:
            raise AuthenticationFailed("Invalid or expired Core access token.") from exc
        if claims.get("token_type") != "access":
            raise AuthenticationFailed("A Core access token is required.")
        try:
            user_id = int(claims["user_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthenticationFailed("Core access token has no valid user_id.") from exc
        user = CoreJWTUser(
            id=user_id,
            email=str(claims.get("email", "")),
            is_staff=bool(claims.get("is_staff", False)),
            is_superuser=bool(claims.get("is_superuser", False)),
        )
        return user, claims
