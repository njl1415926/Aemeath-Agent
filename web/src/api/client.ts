import type { Account, ErrorEnvelope, Message, Page, Session } from "./types";

export class ApiError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  readonly requestId: string | null;
  readonly status: number;

  constructor(status: number, envelope: ErrorEnvelope) {
    super(envelope.error.message);
    this.name = "ApiError";
    this.status = status;
    this.code = envelope.error.code;
    this.retryable = envelope.error.retryable;
    this.requestId = envelope.request_id;
  }
}

let csrfToken: string | null = null;
const authInvalidationListeners = new Set<(error: ApiError) => void>();

const SESSION_INVALIDATION_CODES = new Set([
  "AUTH_REQUIRED",
  "AUTH_INVALID_CREDENTIALS",
  "AUTH_SESSION_EXPIRED",
  "AUTH_SESSION_REVOKED",
  "AUTH_ACCOUNT_DISABLED",
]);

async function parseError(response: Response): Promise<ApiError> {
  let envelope: ErrorEnvelope = {
    error: {
      code: "HTTP_ERROR",
      message: `请求失败（${response.status}）`,
      retryable: response.status >= 500,
    },
    request_id: response.headers.get("X-Request-ID"),
  };
  try {
    envelope = (await response.json()) as ErrorEnvelope;
  } catch {
    // Keep the safe fallback; never surface raw upstream bodies.
  }
  const error = new ApiError(response.status, envelope);
  if (
    ((response.status === 401 && SESSION_INVALIDATION_CODES.has(error.code)) ||
      (response.status === 403 && error.code === "AUTH_ACCOUNT_DISABLED"))
  ) {
    clearCsrfToken();
    for (const listener of authInvalidationListeners) listener(error);
  }
  return error;
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  options: { csrf?: boolean } = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (options.csrf) {
    headers.set("X-CSRF-Token", await getCsrfToken());
  }
  const response = await fetch(path, {
    ...init,
    headers,
    credentials: "include",
  });
  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function clearCsrfToken(): void {
  csrfToken = null;
}

export function subscribeAuthInvalidation(listener: (error: ApiError) => void): () => void {
  authInvalidationListeners.add(listener);
  return () => authInvalidationListeners.delete(listener);
}

export async function getCsrfToken(): Promise<string> {
  if (csrfToken) return csrfToken;
  const result = await request<{ csrf_token: string }>("/v1/auth/csrf");
  csrfToken = result.csrf_token;
  return csrfToken;
}

export const api = {
  me: () => request<Account>("/v1/auth/me"),
  login: async (username: string, password: string) => {
    clearCsrfToken();
    return request<Account>("/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
  },
  acceptInvitation: async (token: string, password: string) => {
    clearCsrfToken();
    return request<Account>("/v1/auth/invitations/accept", {
      method: "POST",
      body: JSON.stringify({ token, password }),
    });
  },
  acceptPasswordReset: async (token: string, newPassword: string) => {
    clearCsrfToken();
    return request<Account>("/v1/auth/password-reset/accept", {
      method: "POST",
      body: JSON.stringify({ token, new_password: newPassword }),
    });
  },
  logout: async () => {
    await request<void>("/v1/auth/logout", { method: "POST" }, { csrf: true });
    clearCsrfToken();
  },
  changePassword: async (currentPassword: string, newPassword: string) => {
    await request<void>(
      "/v1/auth/password/change",
      {
        method: "POST",
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      },
      { csrf: true },
    );
    clearCsrfToken();
  },
  listSessions: (status: Session["status"] | "all" = "all", cursor?: string) => {
    const query = new URLSearchParams({ status, limit: "50" });
    if (cursor) query.set("cursor", cursor);
    return request<Page<Session, "sessions">>(`/v1/sessions?${query}`);
  },
  createSession: (characterId: string) =>
    request<Session>(
      "/v1/sessions",
      {
        method: "POST",
        body: JSON.stringify({ character_id: characterId }),
      },
      { csrf: true },
    ),
  listMessages: (sessionId: string, characterId: string, cursor?: string) => {
    const query = new URLSearchParams({ character_id: characterId, limit: "100" });
    if (cursor) query.set("cursor", cursor);
    return request<Page<Message, "messages">>(
      `/v1/sessions/${encodeURIComponent(sessionId)}/messages?${query}`,
    );
  },
  updateSessionTitle: (sessionId: string, characterId: string, title: string | null) =>
    request<Session>(
      `/v1/sessions/${encodeURIComponent(sessionId)}`,
      {
        method: "PATCH",
        body: JSON.stringify({ character_id: characterId, title }),
      },
      { csrf: true },
    ),
  changeSessionStatus: (
    sessionId: string,
    characterId: string,
    action: "end" | "resume" | "archive",
  ) =>
    request<Session>(
      `/v1/sessions/${encodeURIComponent(sessionId)}/${action}`,
      {
        method: "POST",
        body: JSON.stringify({ character_id: characterId }),
      },
      { csrf: true },
    ),
};

export { parseError };
