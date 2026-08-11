import { getCsrfToken, parseError } from "./client";
import type { StreamEvent } from "./types";

type StreamHandlers = {
  onEvent: (event: StreamEvent) => void;
};

export async function streamChat(
  input: {
    sessionId: string;
    characterId: string;
    message: string;
    clientMessageId: string;
  },
  handlers: StreamHandlers,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch("/v1/chat/stream", {
    method: "POST",
    credentials: "include",
    signal,
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": await getCsrfToken(),
    },
    body: JSON.stringify({
      session_id: input.sessionId,
      character_id: input.characterId,
      message: input.message,
      client_message_id: input.clientMessageId,
      metadata: {},
    }),
  });
  if (!response.ok) throw await parseError(response);
  if (!response.body) throw new Error("stream_response_body_missing");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const parsed = parseSseFrame(frame);
      if (parsed) handlers.onEvent(parsed);
    }
    if (done) break;
  }
  if (buffer.trim()) {
    const parsed = parseSseFrame(buffer);
    if (parsed) handlers.onEvent(parsed);
  }
}

export function parseSseFrame(frame: string): StreamEvent | null {
  if (!frame.trim() || frame.startsWith(":")) return null;
  let eventType = "";
  const data: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) eventType = line.slice(6).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!eventType || data.length === 0) return null;
  const payload = JSON.parse(data.join("\n")) as Record<string, unknown>;
  return { type: eventType, ...payload } as StreamEvent;
}
