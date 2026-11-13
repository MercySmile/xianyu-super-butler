"""MTOP 登录会话与签名令牌必须分别处理。"""


def is_session_expired(value) -> bool:
    text = str(value or "").upper()
    return any(marker in text for marker in (
        "FAIL_SYS_SESSION_EXPIRED", "SESSION过期", "FAIL_SYS_LOGIN_REQUIRED",
    ))


def is_token_expired(value) -> bool:
    text = str(value or "").upper()
    return not is_session_expired(value) and any(marker in text for marker in (
        "FAIL_SYS_TOKEN_EXPIRED", "FAIL_SYS_TOKEN_EXOIRED", "FAIL_SYS_TOKEN_EMPTY", "令牌过期",
    ))
