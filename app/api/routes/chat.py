from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import ApiRuntime, get_runtime, get_user_context
from app.api.schemas import ChatRequestBody, ChatResponseBody
from app.services.schemas import ChatRequest, UserContext

router = APIRouter(prefix="/v1", tags=["chat"])


@router.post("/chat", response_model=ChatResponseBody)
def chat(
    body: ChatRequestBody,
    user_context: UserContext = Depends(get_user_context),
    runtime: ApiRuntime = Depends(get_runtime),
) -> ChatResponseBody:
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
