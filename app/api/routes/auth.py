from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.dependencies import (
    ApiRuntime,
    get_authenticated_browser_session,
    get_csrf_authenticated_browser_session,
    get_runtime,
    require_browser_auth_runtime,
)
from app.api.schemas import (
    CsrfResponse,
    InvitationAcceptBody,
    LoginBody,
    MeResponse,
    PasswordChangeBody,
    PasswordResetAcceptBody,
)
from app.auth.csrf import derive_csrf_token
from app.auth.schemas import AuthenticatedBrowserSession, BrowserLoginResult
from app.runtime.guardrails.errors import ApiRateLimitedError

router = APIRouter(prefix="/v1/auth", tags=["auth"])
_COOKIE_NAME = "__Host-aemeath_session"


@router.post(
    "/invitations/accept",
    response_model=MeResponse,
    status_code=status.HTTP_201_CREATED,
)
def accept_invitation(
    body: InvitationAcceptBody,
    request: Request,
    response: Response,
    runtime: ApiRuntime = Depends(get_runtime),
) -> MeResponse:
    browser = require_browser_auth_runtime(runtime)
    _check_pre_auth(request=request, runtime=runtime, endpoint="invitation_accept")
    result = browser.invitations.accept(
        raw_token=body.token.get_secret_value(),
        password=body.password.get_secret_value(),
        request_id=request.state.request_id,
    )
    _set_session_cookie(response, result.raw_session_token)
    _no_store(response)
    return _me(result)


@router.post("/login", response_model=MeResponse)
def login(
    body: LoginBody,
    request: Request,
    response: Response,
    runtime: ApiRuntime = Depends(get_runtime),
) -> MeResponse:
    browser = require_browser_auth_runtime(runtime)
    _check_pre_auth(
        request=request,
        runtime=runtime,
        endpoint="login",
        identity=body.username,
    )
    result = browser.sessions.login(
        username=body.username,
        password=body.password.get_secret_value(),
        request_id=request.state.request_id,
    )
    _set_session_cookie(response, result.raw_session_token)
    _no_store(response)
    return _me(result)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    authenticated: AuthenticatedBrowserSession = Depends(get_csrf_authenticated_browser_session),
    runtime: ApiRuntime = Depends(get_runtime),
) -> None:
    browser = require_browser_auth_runtime(runtime)
    browser.sessions.logout(web_session_id=authenticated.web_session_id)
    _clear_session_cookie(response)
    _no_store(response)


@router.get("/me", response_model=MeResponse)
def me(
    response: Response,
    authenticated: AuthenticatedBrowserSession = Depends(get_authenticated_browser_session),
) -> MeResponse:
    _no_store(response)
    account = authenticated.account
    return MeResponse(
        account_id=account.id,
        username=account.username,
        display_name=account.display_name,
        roles=list(account.roles),
    )


@router.get("/csrf", response_model=CsrfResponse)
def csrf(
    response: Response,
    authenticated: AuthenticatedBrowserSession = Depends(get_authenticated_browser_session),
    runtime: ApiRuntime = Depends(get_runtime),
) -> CsrfResponse:
    browser = require_browser_auth_runtime(runtime)
    _no_store(response)
    return CsrfResponse(
        csrf_token=derive_csrf_token(
            key=browser.csrf_key,
            web_session_id=authenticated.web_session_id,
            verifier=authenticated.csrf_verifier,
        )
    )


@router.post("/password/change", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: PasswordChangeBody,
    response: Response,
    authenticated: AuthenticatedBrowserSession = Depends(get_csrf_authenticated_browser_session),
    runtime: ApiRuntime = Depends(get_runtime),
) -> None:
    browser = require_browser_auth_runtime(runtime)
    result = browser.sessions.change_password(
        authenticated=authenticated,
        current_password=body.current_password.get_secret_value(),
        new_password=body.new_password.get_secret_value(),
        request_id=authenticated.user_context.request_id,
    )
    _set_session_cookie(response, result.raw_session_token)
    _no_store(response)


@router.post("/password-reset/accept", response_model=MeResponse)
def accept_password_reset(
    body: PasswordResetAcceptBody,
    request: Request,
    response: Response,
    runtime: ApiRuntime = Depends(get_runtime),
) -> MeResponse:
    browser = require_browser_auth_runtime(runtime)
    _check_pre_auth(request=request, runtime=runtime, endpoint="password_reset_accept")
    result = browser.password_resets.accept(
        raw_token=body.token.get_secret_value(),
        new_password=body.new_password.get_secret_value(),
        request_id=request.state.request_id,
    )
    _set_session_cookie(response, result.raw_session_token)
    _no_store(response)
    return _me(result)


def _check_pre_auth(
    *,
    request: Request,
    runtime: ApiRuntime,
    endpoint: str,
    identity: str | None = None,
) -> None:
    if getattr(request.state, "pre_auth_endpoint", None) != endpoint:
        raise RuntimeError("pre_auth_admission_missing")
    if identity is None:
        return
    browser = require_browser_auth_runtime(runtime)
    decision = browser.pre_auth_limiter.check_identity(identity_value=identity)
    if not decision.allowed:
        raise ApiRateLimitedError(
            f"auth_pre_{decision.reason}",
            retry_after_seconds=decision.retry_after_seconds,
        )


def _me(result: BrowserLoginResult) -> MeResponse:
    return MeResponse(
        account_id=result.account.id,
        username=result.account.username,
        display_name=result.account.display_name,
        roles=list(result.account.roles),
    )


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.set_cookie(
        key=_COOKIE_NAME,
        value="",
        max_age=0,
        expires=0,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
