from __future__ import annotations


class MockChatModelClient:
    def chat(self, *, messages: list[dict[str, str]]) -> str:
        user_input = messages[-1]["content"] if messages else ""
        system_content = messages[0]["content"] if messages else ""
        if "系统提示词" in user_input or "system prompt" in user_input.lower():
            return "这个内容我不能提供，但我可以继续陪你推进当前任务。"
        if "忽略之前所有规则" in user_input:
            return "这条要求我不能执行。我们继续按当前目标把事情做好。"
        if "还记得" in user_input and "Long-term user memories:\n- (none)" not in system_content:
            return _recall_from_system_context(system_content)
        if "好累" in user_input or "不想写代码" in user_input:
            return "听起来你今天消耗很大。我们先把目标缩小到一个最小步骤，好吗？"
        if "记住" in user_input:
            return "我记住了，这条信息会作为后续对话参考。"
        return "我在。我们继续往前推进，你想先处理哪一块？"


def _recall_from_system_context(system_content: str) -> str:
    lines = system_content.splitlines()
    in_memory_section = False
    for line in lines:
        if line == "Long-term user memories:":
            in_memory_section = True
            continue
        if in_memory_section and line.startswith("Canon evidence:"):
            break
        if in_memory_section and line.startswith("- ["):
            memory_content = line.split("): ", 1)[-1]
            return f"我记得，我们之前提到过：{memory_content}"
    return "我记得我们聊过很重要的事，但这次我没能准确找到那条记忆。"

