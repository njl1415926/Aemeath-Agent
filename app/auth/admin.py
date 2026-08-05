from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from app.auth.browser_sessions import BrowserSessionAuthService, _format_time
from app.auth.invitations import InvitationService, PasswordResetService
from app.auth.passwords import PasswordEngine
from app.config import Settings
from app.db.store_factory import create_store
from app.observability.noop import NoopAppLogger


def _build_services(settings: Settings, *, origin: str | None):
    _validate_auth_admin_settings(settings)
    resolved_origin = origin or settings.web_public_origin
    if not resolved_origin:
        raise ValueError("web_public_origin_required")
    store = create_store(db_path=settings.db_path, backend=settings.db_backend)
    logger = NoopAppLogger()
    passwords = PasswordEngine(
        concurrency=settings.password_work_concurrency,
        wait_timeout_seconds=settings.password_work_wait_timeout_seconds,
    )
    sessions = BrowserSessionAuthService(
        store=store.browser_auth,
        passwords=passwords,
        app_logger=logger,
        idle_seconds=settings.web_session_idle_seconds,
        absolute_seconds=settings.web_session_absolute_seconds,
        touch_seconds=settings.web_session_touch_seconds,
    )
    invitations = InvitationService(
        store=store.browser_auth,
        passwords=passwords,
        sessions=sessions,
        app_logger=logger,
        public_origin=resolved_origin,
        lifetime_seconds=settings.invitation_ttl_seconds,
    )
    resets = PasswordResetService(
        store=store.browser_auth,
        passwords=passwords,
        sessions=sessions,
        app_logger=logger,
        public_origin=resolved_origin,
        lifetime_seconds=settings.password_reset_ttl_seconds,
    )
    return store, sessions, invitations, resets


def _validate_auth_admin_settings(settings: Settings) -> None:
    if settings.db_backend not in {"sqlite_raw", "sqlalchemy"}:
        raise ValueError("auth_admin_db_backend_invalid")
    if settings.password_profile != "argon2id_owasp_2026_v1":
        raise ValueError("auth_admin_password_profile_invalid")
    if settings.password_work_concurrency != 1:
        raise ValueError("auth_admin_password_concurrency_invalid")
    if settings.password_work_wait_timeout_seconds < 0:
        raise ValueError("auth_admin_password_wait_invalid")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aemeath Browser Auth operator commands.")
    parser.add_argument(
        "--origin",
        help="Exact public origin; defaults to AEMEATH_WEB_PUBLIC_ORIGIN.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create-invite")
    create.add_argument("--username", required=True)
    create.add_argument("--display-name")

    list_invites = subparsers.add_parser("list-invites")
    list_invites.add_argument("--limit", type=int, default=50)

    revoke = subparsers.add_parser("revoke-invite")
    revoke.add_argument("invitation_id")

    issue_reset = subparsers.add_parser("issue-password-reset")
    issue_reset.add_argument("--account", required=True)

    for name in ("disable-account", "enable-account", "revoke-account-sessions"):
        command = subparsers.add_parser(name)
        command.add_argument("account_id")

    cleanup = subparsers.add_parser("cleanup-auth-sessions")
    cleanup.add_argument("--before", required=True)
    cleanup.add_argument("--limit", type=int, default=100)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
        store, sessions, invitations, resets = _build_services(settings, origin=args.origin)
        now = _format_time(sessions._now())
        if args.command == "create-invite":
            issued = invitations.issue(
                username=args.username,
                display_name=args.display_name,
                roles=["user"],
            )
            print("This value will not be shown again:")
            print(issued.invitation_url)
            return 0
        if args.command == "list-invites":
            records = []
            for record in store.browser_auth.list_invitations(limit=args.limit):
                payload = asdict(record)
                if record.status == "pending" and record.expires_at <= now:
                    payload["derived_status"] = "expired"
                else:
                    payload["derived_status"] = record.status
                records.append(payload)
            print(json.dumps(records, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "revoke-invite":
            changed = store.browser_auth.revoke_invitation(
                invitation_id=args.invitation_id, now=now
            )
            print(json.dumps({"changed": changed}, sort_keys=True))
            return 0
        if args.command == "issue-password-reset":
            issued = resets.issue(account_selector=args.account)
            print("This value will not be shown again:")
            print(issued.reset_url)
            return 0
        if args.command == "disable-account":
            account_id = _resolve_account_id(store, args.account_id)
            changed = store.browser_auth.disable_account(account_id=account_id, now=now)
        elif args.command == "enable-account":
            account_id = _resolve_account_id(store, args.account_id)
            changed = store.browser_auth.enable_account(account_id=account_id, now=now)
        elif args.command == "revoke-account-sessions":
            account_id = _resolve_account_id(store, args.account_id)
            count = store.browser_auth.revoke_account_sessions(
                account_id=account_id,
                reason="operator_revoked",
                now=now,
            )
            print(json.dumps({"revoked_count": count}, sort_keys=True))
            return 0
        elif args.command == "cleanup-auth-sessions":
            count = store.browser_auth.cleanup_sessions(before=args.before, limit=args.limit)
            print(json.dumps({"deleted_count": count}, sort_keys=True))
            return 0
        else:
            raise AssertionError("unknown_auth_admin_command")
        print(json.dumps({"changed": changed}, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"auth admin command failed ({type(exc).__name__})", file=sys.stderr)
        return 1


def _resolve_account_id(store, selector: str) -> str:
    account_id = store.browser_auth.resolve_account_id(account_selector=selector)
    if account_id is None:
        raise ValueError("auth_admin_account_unavailable")
    return account_id


if __name__ == "__main__":
    raise SystemExit(main())
