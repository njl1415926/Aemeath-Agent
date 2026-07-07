from __future__ import annotations

from app.permissions.errors import PermissionDeniedError
from app.services.schemas import UserContext


class PermissionService:
    def require_owner(
        self,
        *,
        resource_user_id: str,
        user_context: UserContext,
    ) -> None:
        if resource_user_id != user_context.user_id:
            raise PermissionDeniedError()
