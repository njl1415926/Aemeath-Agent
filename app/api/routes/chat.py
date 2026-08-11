from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import ApiRuntime, get_rate_limited_user_context, get_runtime
from app.api.schemas import ChatRequestBody, ChatResponseBody
from app.errors import AemeathError, ErrorCode
from app.services.schemas import ChatRequest, UserContext

router = APIRouter(prefix="/v1", tags=["chat"])


@router.post("/chat", response_model=ChatResponseBody)
def chat(
    body: ChatRequestBody,
    user_context: UserContext = Depends(get_rate_limited_user_context),
    runtime: ApiRuntime = Depends(get_runtime),
) -> ChatResponseBody:
    if sync_chat_disabled_for_production_web(runtime=runtime):
        raise AemeathError(
            code=ErrorCode.PERMISSION_DENIED,
            public_message="Synchronous chat is disabled for this profile.",
            internal_message="sync_chat_disabled_production_web",
            retryable=False,
            metadata={"reason": "sync_chat_disabled_production_web"},
        )
    response = runtime.agent_service.handle_chat(
        user_context=user_context,
        request=ChatRequest(
            session_id=body.session_id,
            character_id=body.character_id,
            message=body.message,
            metadata=body.metadata,
        ),
    )
    return ChatResponseBody.from_service(response)


def sync_chat_disabled_for_production_web(*, runtime: ApiRuntime) -> bool:
    settings = runtime.settings
    return settings.environment == "prod" and settings.api_auth_mode == "web_session"
