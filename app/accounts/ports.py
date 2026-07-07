from __future__ import annotations

from typing import Protocol

from app.accounts.schemas import AccountRecord, AccountStatus


class AccountStorePort(Protocol):
    def get_account(self, *, account_id: str) -> AccountRecord | None:
        ...

    def get_account_by_dev_token_hash(
        self,
        *,
        dev_token_hash: str,
    ) -> AccountRecord | None:
        ...

    def upsert_account(
        self,
        *,
        account_id: str,
        username: str,
        display_name: str | None,
        status: AccountStatus,
        roles: list[str],
        dev_token_hash: str | None = None,
    ) -> AccountRecord:
        ...

    def set_account_dev_token_hash(
        self,
        *,
        account_id: str,
        dev_token_hash: str | None,
    ) -> AccountRecord:
        ...

    def set_account_status(
        self,
        *,
        account_id: str,
        status: AccountStatus,
    ) -> AccountRecord:
        ...

    def touch_account_login(self, *, account_id: str) -> AccountRecord:
        ...
