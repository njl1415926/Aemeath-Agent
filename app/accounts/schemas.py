from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

AccountStatus = Literal["active", "disabled"]


@dataclass(frozen=True)
class AccountRecord:
    id: str
    username: str
    display_name: str | None
    status: AccountStatus
    roles: list[str] = field(default_factory=list)
    dev_token_hash: str | None = None
    created_at: str = ""
    updated_at: str = ""
    last_login_at: str | None = None
