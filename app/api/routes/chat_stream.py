from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.dependencies import ApiRuntime, get_rate_limited_user_context, get_runtime
from app.api.schemas import StreamChatRequestBody
from app.api.sse import OwnedStreamingResponse
from app.services.schemas import UserContext
from app.services.stream_schemas import StreamChatRequest

router = APIRouter(prefix="/v1", tags=["chat"])


@router.post("/chat/stream")
def chat_stream(
    body: StreamChatRequestBody,
    request: Request,
    user_context: UserContext = Depends(get_rate_limited_user_context),
    runtime: ApiRuntime = Depends(get_runtime),
) -> OwnedStreamingResponse:
    handle = runtime.agent_stream_service.stream_chat(
        user_context=user_context,
        request=StreamChatRequest(
            session_id=body.session_id,
            character_id=body.character_id,
            message=body.message,
            client_message_id=body.client_message_id,
            metadata=body.metadata,
        ),
    )
    try:
        return OwnedStreamingResponse(
            request=request,
            handle=handle,
            app_logger=runtime.app_logger,
        )
    except BaseException:
        handle.cancel(reason="sse_generator_closed")
        handle.close()
        raise
