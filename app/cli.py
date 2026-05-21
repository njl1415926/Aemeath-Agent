from __future__ import annotations

from app.main import build_controller
from app.schemas import AgentTurnInput
from app.utils.ids import new_id

HELP_TEXT = """
Commands:
  /help                         Show help
  /exit                         Exit CLI
  /memory list                  List active memories
  /memory delete <memory_id>    Archive memory
  /memory disable <memory_id>   Disable memory
"""


def run_cli() -> int:
    controller, memory_admin, settings, _store = build_controller()
    session_id = new_id("session")
    user_id = settings.default_user_id
    print("Aemeath Agent v0.1 CLI")
    print("Type /help for commands.")
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
        if user_input.startswith("/memory"):
            print(memory_admin.handle_command(command=user_input, user_id=user_id))
            continue

        turn_input = AgentTurnInput(
            user_id=user_id,
            session_id=session_id,
            character_id=settings.default_character_id,
            message=user_input,
            metadata={"client": "cli"},
        )
        try:
            output = controller.handle_user_message(turn_input)
            print(f"Aemeath> {output.response}")
            if output.warnings:
                print(f"[warning] {', '.join(output.warnings)}")
        except Exception as exc:  # noqa: BLE001
            print(f"Aemeath> 当前处理失败：{exc}")


if __name__ == "__main__":
    raise SystemExit(run_cli())
