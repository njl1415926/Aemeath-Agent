from __future__ import annotations

from app.schemas import StateUpdate


class StateUpdater:
    def __init__(self, classifier):
        self.classifier = classifier

    def decide(self, *, user_input: str, assistant_response: str) -> StateUpdate:
        return self.classifier.decide(
            user_input=user_input,
            assistant_response=assistant_response,
        )
