"""Tests for reMarkable Cloud authentication."""

import json
import time
from base64 import b64encode
from unittest.mock import AsyncMock, patch

import pytest

from src.remarkable.auth import AuthError, AuthManager, _parse_jwt_expiry

# -- Helpers --


def _make_jwt(payload: dict, header: dict | None = None) -> str:
    """Build a fake JWT (no signature verification needed)."""
    header = header or {"alg": "HS256", "typ": "JWT"}
    h = b64encode(json.dumps(header).encode()).decode().rstrip("=")
    p = b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{h}.{p}.fake_signature"


# -- _parse_jwt_expiry --


class TestParseJwtExpiry:
    def test_valid_jwt(self):
        exp = int(time.time()) + 3600
        token = _make_jwt({"exp": exp, "sub": "user123"})
        assert _parse_jwt_expiry(token) == float(exp)

    def test_missing_exp_claim(self):
        token = _make_jwt({"sub": "user123"})
        assert _parse_jwt_expiry(token) == 0

    def test_garbage_input(self):
        assert _parse_jwt_expiry("not.a.jwt") == 0
        assert _parse_jwt_expiry("") == 0
        assert _parse_jwt_expiry("no-dots") == 0

    def test_malformed_base64(self):
        assert _parse_jwt_expiry("a.!!!invalid!!!.b") == 0


# -- AuthManager --


class TestAuthManager:
    def test_load_missing_token_raises(self, tmp_path):
        auth = AuthManager(tmp_path / "nonexistent_token")
        with pytest.raises(AuthError, match="No device token found"):
            _ = auth.device_token

    def test_load_empty_token_raises(self, tmp_path):
        token_file = tmp_path / "device_token"
        token_file.write_text("")
        auth = AuthManager(token_file)
        with pytest.raises(AuthError, match="empty"):
            _ = auth.device_token

    def test_load_valid_token(self, tmp_path):
        token_file = tmp_path / "device_token"
        token_file.write_text("my-device-token-123")
        auth = AuthManager(token_file)
        assert auth.device_token == "my-device-token-123"

    def test_has_device_token(self, tmp_path):
        auth = AuthManager(tmp_path / "nope")
        assert auth.has_device_token() is False

        token_file = tmp_path / "token"
        token_file.write_text("exists")
        auth2 = AuthManager(token_file)
        assert auth2.has_device_token() is True

    def test_save_device_token(self, tmp_path):
        token_file = tmp_path / "subdir" / "device_token"
        auth = AuthManager(token_file)
        auth._save_device_token("saved-token-456")

        assert token_file.exists()
        assert token_file.read_text() == "saved-token-456"
        # Check permissions (owner-only)
        assert oct(token_file.stat().st_mode)[-3:] == "600"

    @pytest.mark.asyncio
    async def test_register_device_success(self, tmp_path):
        token_file = tmp_path / "device_token"
        auth = AuthManager(token_file)

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.text = "new-device-token-789"

        with patch("src.remarkable.auth.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            token = await auth.register_device("one-time-code")

        assert token == "new-device-token-789"
        assert token_file.read_text() == "new-device-token-789"

    @pytest.mark.asyncio
    async def test_register_device_failure(self, tmp_path):
        auth = AuthManager(tmp_path / "token")

        mock_response = AsyncMock()
        mock_response.status_code = 400
        mock_response.text = "Invalid code"

        with patch("src.remarkable.auth.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            with pytest.raises(AuthError, match="registration failed"):
                await auth.register_device("bad-code")

    @pytest.mark.asyncio
    async def test_get_user_token_caches(self, tmp_path):
        token_file = tmp_path / "device_token"
        token_file.write_text("device-token")
        auth = AuthManager(token_file)

        exp = int(time.time()) + 7200  # 2 hours from now
        user_jwt = _make_jwt({"exp": exp})

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.text = user_jwt

        with patch("src.remarkable.auth.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            # First call — hits the API
            t1 = await auth.get_user_token()
            assert t1 == user_jwt

            # Second call — should use cached token, no extra API call
            t2 = await auth.get_user_token()
            assert t2 == user_jwt
            assert mock_client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_get_user_token_refreshes_when_expired(self, tmp_path):
        token_file = tmp_path / "device_token"
        token_file.write_text("device-token")
        auth = AuthManager(token_file)

        # Set an already-expired cached token
        auth._user_token = "old-expired-token"
        auth._user_token_expiry = time.time() - 100

        exp = int(time.time()) + 7200
        fresh_jwt = _make_jwt({"exp": exp})

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.text = fresh_jwt

        with patch("src.remarkable.auth.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            token = await auth.get_user_token()
            assert token == fresh_jwt
