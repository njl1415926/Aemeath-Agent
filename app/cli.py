from __future__ import annotations

from app.errors import map_exception
from app.main import build_application_runtime
from app.runtime.resources import ApplicationRuntime
from app.services.schemas import ChatRequest, UserContext
from app.session.errors import SessionError
from app.session.ports import SessionStorePort

HELP_TEXT = """
Commands:
  /help                         Show help
  /exit                         Exit CLI
  /session current              Show the current session
  /session new                  Create and select a new session
  /session list                 List active sessions
  /session resume <session_id>  Resume an ended session and show recent history
  /session end [session_id]     End a session
  /session archive [session_id] Archive a session
  /memory list                  List active memories
  /memory delete <memory_id>    Archive memory
  /memory disable <memory_id>   Disable memory
  /memory extract [limit]       Extract long-term memories from recent messages
  /memory debug on|off          Show recalled long-term memories after each turn
"""


def _format_session(session) -> str:
    last_message_at = session.last_message_at or "-"
    return (
        f"{session.id} status={session.status} character={session.character_id} "
        f"messages={session.message_count} last={last_message_at}"
    )


def _new_cli_session(
    *,
    session_store: SessionStorePort,
    user_id: str,
    character_id: str,
):
    return session_store.create_session(
        user_id=user_id,
        character_id=character_id,
        metadata={"client": "cli"},
    )


def _handle_session_command(
    *,
    command: str,
    session_store: SessionStorePort,
    message_store,
    current_session_id: str,
    user_id: str,
    character_id: str,
    history_limit: int,
) -> str:
    parts = command.split()
    action = parts[1] if len(parts) > 1 else ""

    if action == "current" and len(parts) == 2:
        session = session_store.get_session(session_id=current_session_id)
        if session is None or session.user_id != user_id:
            raise SessionError("current session is unavailable")
        print(f"[session current] {_format_session(session)}")
        return current_session_id

    if action == "new" and len(parts) == 2:
        session = _new_cli_session(
            session_store=session_store,
            user_id=user_id,
            character_id=character_id,
        )
        print(f"[session new] {_format_session(session)}")
        return session.id

    if action == "list" and len(parts) == 2:
        sessions = session_store.list_sessions(
            user_id=user_id,
            character_id=character_id,
            status="active",
        )
        print("[session list]")
        if not sessions:
            print("none")
        for session in sessions:
            marker = "*" if session.id == current_session_id else " "
            print(f"{marker} {_format_session(session)}")
        return current_session_id

    if action == "resume" and len(parts) == 3:
        session = session_store.resume_session(
            session_id=parts[2],
            user_id=user_id,
            character_id=character_id,
        )
        print(f"[session resume] {_format_session(session)}")
        history = message_store.get_recent_messages(
            session_id=session.id,
            limit=history_limit,
        )
        print("[history]")
        if not history:
            print("none")
        for message in history:
            print(f"{message.role}> {message.content}")
        return session.id

    if action in {"end", "archive"} and len(parts) in {2, 3}:
        target_id = parts[2] if len(parts) == 3 else current_session_id
        if action == "end":
            session = session_store.end_session(
                session_id=target_id,
                user_id=user_id,
            )
        else:
            session = session_store.archive_session(
                session_id=target_id,
                user_id=user_id,
            )
        print(f"[session {action}] {_format_session(session)}")
        if target_id != current_session_id:
            return current_session_id
        replacement = _new_cli_session(
            session_store=session_store,
            user_id=user_id,
            character_id=character_id,
        )
        print(f"[session current] {_format_session(replacement)}")
        return replacement.id

    raise SessionError(
        "usage: /session current|new|list|resume <session_id>|"
        "end [session_id]|archive [session_id]"
    )


def run_cli() -> int:
    runtime = build_application_runtime(surface="cli")
    try:
        return _run_cli(runtime)
    finally:
        runtime.close()


def _run_cli(runtime: ApplicationRuntime) -> int:
    agent_service = runtime.agent_service
    memory_admin = runtime.memory_admin_service
    settings = runtime.settings
    store = runtime.store
    user_id = settings.default_user_id
    current_session = _new_cli_session(
        session_store=store,
        user_id=user_id,
        character_id=settings.default_character_id,
    )
    session_id = current_session.id
    memory_debug_enabled = False
    print("Aemeath Agent CLI")
    print("Type /help for commands.")
    print(f"[session current] {_format_session(current_session)}")
    while True:
        try:
            user_input = input("\nYou> ").strip()
        except EOFError:
            print("\nbye.")
            return 0

        if not user_input:
            continue
        if user_input == "/help":
            print(HELP_TEXT.strip())
            continue
        if user_input == "/exit":
            print("bye.")
            return 0
        if user_input.startswith("/session"):
            try:
                session_id = _handle_session_command(
                    command=user_input,
                    session_store=store,
                    message_store=store,
                    current_session_id=session_id,
                    user_id=user_id,
                    character_id=settings.default_character_id,
                    history_limit=settings.recent_message_limit,
                )
            except SessionError as exc:
                error_info = map_exception(exc)
                print(
                    f"[session] command failed: {error_info.public_message} "
                    f"[{error_info.code.value}]"
                )
            continue
        if user_input.startswith("/memory debug"):
            parts = user_input.split()
            if len(parts) == 3 and parts[2] in {"on", "off"}:
                memory_debug_enabled = parts[2] == "on"
                status = "enabled" if memory_debug_enabled else "disabled"
                print(f"[memory debug] {status}")
            else:
                print("usage: /memory debug on|off")
            continue
        if user_input.startswith("/memory"):
            try:
                print(
                    memory_admin.handle_command(
                        command=user_input,
                        user_id=user_id,
                        session_id=session_id,
                        character_id=settings.default_character_id,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                error_info = map_exception(exc)
                print(
                    f"[memory] command failed: {error_info.public_message} "
                    f"[{error_info.code.value}]"
                )
            continue

        chat_request = ChatRequest(
            session_id=session_id,
            character_id=settings.default_character_id,
            message=user_input,
            metadata={"client": "cli"},
        )
        try:
            output = agent_service.handle_chat(
                user_context=UserContext(user_id=user_id),
                request=chat_request,
            )
            if memory_debug_enabled:
                if output.used_memories:
                    print("[memory recall]")
                    for memory in output.used_memories:
                        score = "none" if memory.score is None else f"{memory.score:.3f}"
                        print(
                            f"{memory.id} score={score} "
                            f"reason={memory.match_reason}\n- {memory.content}"
                        )
                else:
                    print("[memory recall]\nnone")
            print(f"Aemeath> {output.response}")
            if output.warnings:
                print(f"[warning] {', '.join(output.warnings)}")
        except Exception as exc:  # noqa: BLE001
            error_info = map_exception(exc)
            print(
                f"Aemeath> 当前处理失败：{error_info.public_message} "
                f"[{error_info.code.value}]"
            )


if __name__ == "__main__":
    raise SystemExit(run_cli())
