from __future__ import annotations

from app.accounts.ports import AccountStorePort
from app.accounts.schemas import AccountRecord, AccountStatus

DEFAULT_ACCOUNT_ID = "user_default"
DEFAULT_ACCOUNT_USERNAME = "user_default"
DEFAULT_ACCOUNT_DISPLAY_NAME = "Default User"
DEFAULT_ACCOUNT_ROLES = ["user"]


class AccountService:
    def __init__(self, store: AccountStorePort):
        self.store = store

    def ensure_default_account(self) -> AccountRecord:
        return self.ensure_account(
            account_id=DEFAULT_ACCOUNT_ID,
            username=DEFAULT_ACCOUNT_USERNAME,
            display_name=DEFAULT_ACCOUNT_DISPLAY_NAME,
        )

    def ensure_account(
        self,
        *,
        account_id: str,
        username: str | None = None,
        display_name: str | None = None,
        roles: list[str] | None = None,
        status: AccountStatus = "active",
    ) -> AccountRecord:
        existing = self.store.get_account(account_id=account_id)
        if existing is not None:
            return existing
        return self.store.upsert_account(
            account_id=account_id,
            username=username or account_id,
            display_name=display_name or _default_display_name(account_id),
            status=status,
            roles=roles or DEFAULT_ACCOUNT_ROLES,
        )

    def set_dev_token_hash(
        self,
        *,
        account_id: str,
        dev_token_hash: str | None,
    ) -> AccountRecord:
        self.ensure_account(account_id=account_id)
        return self.store.set_account_dev_token_hash(
            account_id=account_id,
            dev_token_hash=dev_token_hash,
        )

    def get_by_dev_token_hash(self, *, dev_token_hash: str) -> AccountRecord | None:
        return self.store.get_account_by_dev_token_hash(dev_token_hash=dev_token_hash)

    def get_account(self, *, account_id: str) -> AccountRecord | None:
        return self.store.get_account(account_id=account_id)

    def record_login(self, *, account_id: str) -> AccountRecord:
        return self.store.touch_account_login(account_id=account_id)


def _default_display_name(account_id: str) -> str:
    if account_id == DEFAULT_ACCOUNT_ID:
        return DEFAULT_ACCOUNT_DISPLAY_NAME
    return account_id
