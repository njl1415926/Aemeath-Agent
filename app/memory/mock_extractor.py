from __future__ import annotations

import json
import re


class MockMemoryExtractionModelClient:
    def chat(self, *, messages: list[dict[str, str]]) -> str:
        user_payload = messages[-1]["content"] if messages else ""
        contents = re.findall(r'"content":\s*"([^"]+)"', user_payload)
        memories = []
        for content in contents:
            if any(trigger in content for trigger in ["记住", "请记住", "帮我记一下"]):
                cleaned = (
                    content.replace("请记住", "")
                    .replace("帮我记一下", "")
                    .replace("记住", "")
                    .strip(" ，,:：。")
                )
                if cleaned:
                    memories.append(
                        {
                            "content": cleaned,
                            "memory_type": "explicit_note",
                            "importance": 8,
                            "confidence": 0.9,
                            "source_message_ids": [],
                            "reason": "mock explicit memory extraction",
                        }
                    )
        return json.dumps({"memories": memories}, ensure_ascii=False)
