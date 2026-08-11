export type Account = {
  account_id: string;
  username: string;
  display_name: string | null;
  roles: string[];
};

export type SessionStatus = "active" | "ended" | "archived";

export type Session = {
  id: string;
  user_id: string;
  character_id: string;
  status: SessionStatus;
  title: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  ended_at: string | null;
  archived_at: string | null;
  last_message_at: string | null;
  message_count: number;
};

export type Message = {
  id: string;
  session_id: string;
  user_id: string;
  role: string;
  content: string;
  created_at: string;
  emotion_label: string | null;
  importance_score: number | null;
};

export type Page<T, K extends string> = Record<K, T[]> & {
  next_cursor: string | null;
  has_more: boolean;
};

export type PublicError = {
  code: string;
  message: string;
  retryable: boolean;
};

export type ErrorEnvelope = {
  error: PublicError;
  request_id: string | null;
};

export type StreamEvent =
  | {
      type: "turn.started";
      request_id: string;
      turn_id: string;
      session_id: string;
    }
  | { type: "response.delta"; delta: string }
  | {
      type: "turn.completed";
      turn_id: string;
      status: "completed";
      warnings: string[];
      state_delta: Record<string, number>;
    }
  | { type: "turn.failed"; turn_id: string; error: PublicError };
