from __future__ import annotations

from app.accounts.schemas import AccountRecord, AccountStatus
from app.db.models import AccountModel
from app.db.repositories.common import SessionFactory, dump_json, load_json_list
from app.utils.ids import utc_now

DEFAULT_ACCOUNT_ID = "user_default"
DEFAULT_ACCOUNT_ROLES = ["user"]


class AccountRepository:
    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory

    def bootstrap_default_account(self) -> None:
        if self.get_account(account_id=DEFAULT_ACCOUNT_ID) is not None:
            return
        self.upsert_account(
            account_id=DEFAULT_ACCOUNT_ID,
            username=DEFAULT_ACCOUNT_ID,
            display_name="Default User",
            status="active",
            roles=DEFAULT_ACCOUNT_ROLES,
        )

    def _from_model(self, model: AccountModel) -> AccountRecord:
        return AccountRecord(
            id=model.id,
            username=model.username,
            username_normalized=model.username_normalized,
            display_name=model.display_name,
            status=model.status,  # type: ignore[arg-type]
            roles=[str(item) for item in load_json_list(model.roles_json)],
            dev_token_hash=model.dev_token_hash,
            created_at=model.created_at,
            updated_at=model.updated_at,
            last_login_at=model.last_login_at,
        )

    def get_account(self, *, account_id: str) -> AccountRecord | None:
        with self._session_factory() as session:
            account = session.get(AccountModel, account_id)
            return self._from_model(account) if account else None

    def get_account_by_dev_token_hash(
        self,
        *,
        dev_token_hash: str,
    ) -> AccountRecord | None:
        with self._session_factory() as session:
            account = (
                session.query(AccountModel)
                .filter(AccountModel.dev_token_hash == dev_token_hash)
                .limit(1)
                .one_or_none()
            )
            return self._from_model(account) if account else None

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
        now = utc_now()
        roles_json = dump_json(roles)
        with self._session_factory() as session:
            account = session.get(AccountModel, account_id)
            if account is None:
                account = AccountModel(
                    id=account_id,
                    username=username,
                    username_normalized=username.lower(),
                    display_name=display_name,
                    status=status,
                    roles_json=roles_json,
                    dev_token_hash=dev_token_hash,
                    created_at=now,
                    updated_at=now,
                    last_login_at=None,
                )
                session.add(account)
            else:
                account.display_name = display_name
                account.status = status
                account.roles_json = roles_json
                account.dev_token_hash = dev_token_hash
                account.updated_at = now
            session.commit()
            return self._from_model(account)

    def set_account_dev_token_hash(
        self,
        *,
        account_id: str,
        dev_token_hash: str | None,
    ) -> AccountRecord:
        with self._session_factory() as session:
            account = session.get(AccountModel, account_id)
            if account is None:
                raise ValueError(f"account {account_id!r} was not found")
            account.dev_token_hash = dev_token_hash
            account.updated_at = utc_now()
            session.commit()
            return self._from_model(account)

    def set_account_status(
        self,
        *,
        account_id: str,
        status: AccountStatus,
    ) -> AccountRecord:
        with self._session_factory() as session:
            account = session.get(AccountModel, account_id)
            if account is None:
                raise ValueError(f"account {account_id!r} was not found")
            account.status = status
            account.updated_at = utc_now()
            session.commit()
            return self._from_model(account)

    def touch_account_login(self, *, account_id: str) -> AccountRecord:
        now = utc_now()
        with self._session_factory() as session:
            account = session.get(AccountModel, account_id)
            if account is None:
                raise ValueError(f"account {account_id!r} was not found")
            account.last_login_at = now
            account.updated_at = now
            session.commit()
            return self._from_model(account)
