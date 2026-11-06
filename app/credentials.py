"""Server-only rotating credentials and a hidden-input maintenance command."""

import argparse
import base64
import getpass
import json
import math
import time
import warnings
from dataclasses import asdict, dataclass, field

import httpx

from .errors import AppError
from .platform_transcript import APP_USER_AGENT, validate_access_token
from .state import RedisStore, create_store, lease, unavailable

REFRESH_URL = "https://api.xiaoyuzhoufm.com/app_auth_tokens.refresh"
CREDENTIAL_KEY = "credentials"
LOCK_KEY = "credentials-lock"
AUTH_MESSAGE = "服务账号登录已失效，请联系维护者重新登录并更新服务凭据。"


def token_expiry(token):
    """Use exp only as a refresh hint, never to establish authenticity."""
    try:
        part = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        value = payload.get("exp")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
    except (IndexError, ValueError, TypeError, AttributeError, UnicodeError):
        pass
    return None


@dataclass(repr=False)
class Credentials:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float | None = None
    retry_after: float = 0
    recovery_required: bool = False

    @classmethod
    def load(cls, value):
        try:
            result = cls(**value)
            validate_access_token(result.refresh_token)
            if result.access_token:
                validate_access_token(result.access_token)
            for number in (result.expires_at, result.retry_after):
                if number is not None and (isinstance(number, bool) or not isinstance(number, (float, int))
                                           or not math.isfinite(number)):
                    raise ValueError
            if not isinstance(result.recovery_required, bool):
                raise ValueError
            return result
        except (TypeError, ValueError, AttributeError, AppError):
            raise unavailable() from None


def refresh_credentials(current):
    headers = {
        "User-Agent": APP_USER_AGENT,
        "x-jike-app-version": "2.7.0",
        "x-jike-device-id": "00000000-0000-0000-0000-000000000000",
        "Content-Type": "application/json",
        "x-jike-refresh-token": current.refresh_token,
    }
    try:
        with httpx.Client(timeout=httpx.Timeout(8, connect=4), follow_redirects=False) as client:
            # The protocol returns credentials in headers; never read or log its body.
            with client.stream("POST", REFRESH_URL, headers=headers, content=b"") as response:
                if response.status_code in (401, 403):
                    raise AppError("SHARED_AUTH_RECOVERY_REQUIRED", AUTH_MESSAGE)
                if response.status_code != 200:
                    raise AppError("SHARED_AUTH_REFRESH_FAILED", "服务登录续期暂不可用，请稍后重试。")
                access = response.headers.get("x-jike-access-token", "")
                refresh = response.headers.get("x-jike-refresh-token") or current.refresh_token
                try:
                    refresh = validate_access_token(refresh)
                    if not access and refresh != current.refresh_token:
                        # Preserve a rotated refresh token even on an incomplete response.
                        return Credentials("", refresh, retry_after=time.time() + 60)
                    access = validate_access_token(access)
                except AppError:
                    raise AppError("SHARED_AUTH_REFRESH_FAILED", "服务登录续期响应异常，请联系维护者。") from None
                return Credentials(access, refresh, token_expiry(access))
    except httpx.HTTPError:
        raise AppError("SHARED_AUTH_REFRESH_FAILED", "服务登录续期连接失败，请稍后重试。") from None


class CredentialManager:
    def __init__(self, store):
        self.store = store

    def read(self):
        value = self.store.get(CREDENTIAL_KEY)
        if value is None:
            raise AppError("SHARED_AUTH_UNCONFIGURED", "服务尚未配置账号凭据，请联系维护者。")
        return Credentials.load(value)

    def access(self, rejected=None):
        current = self.read()
        if current.recovery_required:
            raise AppError("SHARED_AUTH_RECOVERY_REQUIRED", AUTH_MESSAGE)
        if self.usable(current, rejected):
            return current.access_token
        if current.retry_after > time.time():
            raise AppError("SHARED_AUTH_REFRESH_FAILED", "服务正在等待登录续期恢复，请稍后重试。")
        with lease(self.store, LOCK_KEY) as owner:
            current = self.read()
            if current.recovery_required:
                raise AppError("SHARED_AUTH_RECOVERY_REQUIRED", AUTH_MESSAGE)
            if self.usable(current, rejected):
                return current.access_token
            if current.retry_after > time.time():
                raise AppError("SHARED_AUTH_REFRESH_FAILED", "服务正在等待登录续期恢复，请稍后重试。")
            try:
                updated = refresh_credentials(current)
            except AppError as error:
                current.retry_after = time.time() + 60
                current.recovery_required = error.code == "SHARED_AUTH_RECOVERY_REQUIRED"
                self.store.put_locked(CREDENTIAL_KEY, asdict(current), LOCK_KEY, owner)
                raise
            # Save the entire pair before allowing the new access token to be used.
            self.store.put_locked(CREDENTIAL_KEY, asdict(updated), LOCK_KEY, owner)
            if not updated.access_token:
                raise AppError("SHARED_AUTH_REFRESH_FAILED", "服务登录续期响应异常，请稍后重试或联系维护者。")
            return updated.access_token

    @staticmethod
    def usable(current, rejected):
        return bool(current.access_token and current.access_token != rejected
                    and (current.expires_at is None or current.expires_at > time.time() + 60))

    def import_credentials(self, access, refresh):
        refresh = validate_access_token(refresh)
        access = validate_access_token(access) if access.strip() else ""
        with lease(self.store, LOCK_KEY) as owner:
            self.store.put_locked(CREDENTIAL_KEY, asdict(Credentials(access, refresh, token_expiry(access))),
                                  LOCK_KEY, owner)

    def defer_rejected_access(self, rejected):
        """Back off when a freshly issued access token is rejected too."""
        with lease(self.store, LOCK_KEY) as owner:
            current = self.read()
            if current.access_token == rejected:
                current.access_token = ""
                current.expires_at = None
                current.retry_after = time.time() + 60
                self.store.put_locked(CREDENTIAL_KEY, asdict(current), LOCK_KEY, owner)


def main():
    parser = argparse.ArgumentParser(description="安全录入服务账号凭据；输入内容不会显示或写入命令历史。")
    parser.add_argument("--redis", action="store_true", help="提示输入线上 Redis URL，向线上存储录入凭据")
    args = parser.parse_args()
    try:
        warnings.simplefilter("error", getpass.GetPassWarning)
        store = RedisStore(getpass.getpass("线上 Redis URL: ").strip()) if args.redis else create_store()
        access = getpass.getpass("Access token（可留空，首次请求时刷新）: ")
        refresh = getpass.getpass("Refresh token（必填）: ")
        CredentialManager(store).import_credentials(access, refresh)
    except (AppError, EOFError, KeyboardInterrupt, getpass.GetPassWarning) as error:
        print(str(error) if isinstance(error, AppError) else "凭据录入已取消。")
        return 1
    print("服务凭据已保存。首次读取将验证平台鉴权；请勿把凭据发送到聊天或提交到 Git。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
