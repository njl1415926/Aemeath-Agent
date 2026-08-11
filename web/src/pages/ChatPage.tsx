import {
  Archive,
  Check,
  CircleStop,
  LogOut,
  Menu,
  MessageSquarePlus,
  MoreHorizontal,
  PanelLeftClose,
  Pencil,
  Play,
  Send,
  Settings,
  Square,
  X,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";

import { api } from "../api/client";
import { streamChat } from "../api/stream";
import type { Message, Session, StreamEvent } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { FormError } from "../components/FormError";
import { characterVisual } from "../config/character";

type UiMessage = Message & { pending?: boolean; failed?: boolean };

export function ChatPage() {
  const { account, logout, changePassword } = useAuth();
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [loadingSessions, setLoadingSessions] = useState(true);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [accountOpen, setAccountOpen] = useState(false);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [menuSessionId, setMenuSessionId] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const messageEndRef = useRef<HTMLDivElement | null>(null);

  const selected = useMemo(
    () => sessions.find((session) => session.id === selectedId) ?? null,
    [selectedId, sessions],
  );

  const loadSessions = useCallback(async (preferId?: string) => {
    const page = await api.listSessions("all");
    setSessions(page.sessions);
    setSelectedId((current) => {
      const candidate = preferId || current;
      if (candidate && page.sessions.some((session) => session.id === candidate)) {
        return candidate;
      }
      return (
        page.sessions.find((session) => session.status === "active")?.id ??
        page.sessions[0]?.id ??
        null
      );
    });
  }, []);

  useEffect(() => {
    loadSessions()
      .catch(setError)
      .finally(() => setLoadingSessions(false));
  }, [loadSessions]);

  const loadMessages = useCallback(async (
    session: Session,
    options: { clearError?: boolean } = {},
  ) => {
    setLoadingMessages(true);
    if (options.clearError !== false) setError(null);
    try {
      const page = await api.listMessages(session.id, session.character_id);
      setMessages(sortMessages(page.messages));
    } catch (caught) {
      setError(caught);
      setMessages([]);
    } finally {
      setLoadingMessages(false);
    }
  }, []);

  useEffect(() => {
    if (selected) void loadMessages(selected);
    else setMessages([]);
  }, [loadMessages, selected?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    messageEndRef.current?.scrollIntoView({ behavior: streaming ? "auto" : "smooth" });
  }, [messages, streaming]);

  useEffect(
    () => () => {
      abortRef.current?.abort();
    },
    [],
  );

  async function createSession() {
    setError(null);
    try {
      const session = await api.createSession(characterVisual.id);
      await loadSessions(session.id);
      setSidebarOpen(false);
    } catch (caught) {
      setError(caught);
    }
  }

  async function sendMessage(event: FormEvent) {
    event.preventDefault();
    const content = draft.trim();
    if (!content || streaming) return;

    let target = selected;
    if (!target) {
      try {
        target = await api.createSession(characterVisual.id);
        await loadSessions(target.id);
      } catch (caught) {
        setError(caught);
        return;
      }
    }
    if (target.status !== "active") {
      setError(new Error("这个会话当前不能发送消息，请先恢复或新建会话。"));
      return;
    }

    const clientMessageId = `msg_${crypto.randomUUID()}`;
    const assistantId = `pending_${crypto.randomUUID()}`;
    const now = new Date().toISOString();
    setDraft("");
    setError(null);
    setStreaming(true);
    setMessages((current) => [
      ...current,
      localMessage(clientMessageId, target!.id, account!.account_id, "user", content, now),
      {
        ...localMessage(
          assistantId,
          target!.id,
          account!.account_id,
          "assistant",
          "",
          now,
        ),
        pending: true,
      },
    ]);

    const controller = new AbortController();
    abortRef.current = controller;
    let streamOutcome: StreamOutcome = "none";
    try {
      await streamChat(
        {
          sessionId: target.id,
          characterId: target.character_id,
          message: content,
          clientMessageId,
        },
        {
          onEvent: (streamEvent) => {
            if (streamEvent.type === "response.delta") {
              setMessages((current) =>
                current.map((message) =>
                  message.id === assistantId
                    ? { ...message, content: message.content + streamEvent.delta }
                    : message,
                ),
              );
            }
            if (streamEvent.type === "turn.failed") {
              streamOutcome = "failed";
              setMessages((current) =>
                current.map((message) =>
                  message.id === assistantId
                    ? { ...message, pending: false, failed: true }
                    : message,
                ),
              );
              setError(new Error(streamEvent.error.message));
            }
            if (streamEvent.type === "turn.completed") streamOutcome = "completed";
          },
        },
        controller.signal,
      );
      if (streamOutcome === "none") {
        throw new Error("连接提前结束，未收到完整回复。请检查会话记录。 ");
      }
      await loadMessages(target, {
        clearError: shouldClearErrorAfterStream(streamOutcome),
      });
      await loadSessions(target.id);
    } catch (caught) {
      if ((caught as Error)?.name === "AbortError") {
        setError(new Error("已停止生成。已完成的内容会以服务器记录为准。"));
      } else {
        setError(caught);
      }
      setMessages((current) =>
        current.map((message) =>
          message.id === assistantId ? { ...message, pending: false, failed: true } : message,
        ),
      );
    } finally {
      abortRef.current = null;
      setStreaming(false);
    }
  }

  function stopStreaming() {
    abortRef.current?.abort();
  }

  async function renameSession(session: Session, title: string) {
    try {
      await api.updateSessionTitle(session.id, session.character_id, title.trim() || null);
      await loadSessions(session.id);
      setRenamingId(null);
    } catch (caught) {
      setError(caught);
    }
  }

  async function lifecycle(session: Session, action: "end" | "resume" | "archive") {
    setMenuSessionId(null);
    try {
      await api.changeSessionStatus(session.id, session.character_id, action);
      await loadSessions(session.id);
    } catch (caught) {
      setError(caught);
    }
  }

  return (
    <main className="chat-workspace" data-testid="chat-workspace">
      <div className="workspace-scrim" />
      <aside className={`session-sidebar ${sidebarOpen ? "is-open" : ""}`}>
        <div className="sidebar-heading">
          <div className="brand-mark">AE</div>
          <div>
            <strong>Aemeath</strong>
            <span>漂泊者通讯终端</span>
          </div>
          <button
            className="icon-button mobile-only"
            type="button"
            onClick={() => setSidebarOpen(false)}
            title="关闭会话列表"
          >
            <PanelLeftClose size={19} />
          </button>
        </div>
        <button className="new-session-button" type="button" onClick={createSession}>
          <MessageSquarePlus size={18} />
          <span>新建对话</span>
        </button>
        <div className="session-list" aria-label="会话列表">
          {loadingSessions ? <p className="list-empty">正在读取通讯记录...</p> : null}
          {!loadingSessions && sessions.length === 0 ? (
            <p className="list-empty">还没有对话。开启第一条通讯吧。</p>
          ) : null}
          {sessions.map((session) => (
            <SessionRow
              key={session.id}
              session={session}
              selected={session.id === selectedId}
              renaming={session.id === renamingId}
              menuOpen={session.id === menuSessionId}
              disabled={streaming}
              onSelect={() => {
                setSelectedId(session.id);
                setSidebarOpen(false);
              }}
              onBeginRename={() => {
                setRenamingId(session.id);
                setMenuSessionId(null);
              }}
              onRename={(title) => renameSession(session, title)}
              onToggleMenu={() =>
                setMenuSessionId((current) => (current === session.id ? null : session.id))
              }
              onLifecycle={(action) => lifecycle(session, action)}
            />
          ))}
        </div>
        <button className="account-summary" type="button" onClick={() => setAccountOpen(true)}>
          <span className="account-avatar">{account?.display_name?.[0] || account?.username[0]}</span>
          <span>
            <strong>{account?.display_name || account?.username}</strong>
            <small>账号设置</small>
          </span>
          <Settings size={17} />
        </button>
      </aside>

      {sidebarOpen ? (
        <button
          className="sidebar-backdrop mobile-only"
          aria-label="关闭会话列表"
          onClick={() => setSidebarOpen(false)}
        />
      ) : null}

      <section className="conversation-panel">
        <header className="conversation-header">
          <button
            className="icon-button mobile-only"
            type="button"
            onClick={() => setSidebarOpen(true)}
            title="打开会话列表"
          >
            <Menu size={21} />
          </button>
          <img className="character-avatar" src={characterVisual.avatarUrl} alt="" />
          <div className="character-heading">
            <h1>{characterVisual.displayName}</h1>
            <span className="presence"><i />{characterVisual.statusText}</span>
          </div>
          <div className="header-session-state">
            {selected ? <span className={`status-label status-${selected.status}`}>{statusText(selected.status)}</span> : null}
          </div>
        </header>

        <div className="message-scroller" aria-live="polite">
          {loadingMessages ? <div className="empty-conversation">正在展开通讯记录...</div> : null}
          {!loadingMessages && !selected ? (
            <EmptyConversation onStart={createSession} />
          ) : null}
          {!loadingMessages && selected && messages.length === 0 ? (
            <EmptyConversation onStart={() => document.getElementById("chat-composer")?.focus()} />
          ) : null}
          {messages.map((message) => (
            <MessageBubble key={message.id} message={message} />
          ))}
          <div ref={messageEndRef} />
        </div>

        <div className="composer-area">
          {error ? (
            <div className="chat-error-row">
              <FormError error={error} />
              <button className="icon-button" onClick={() => setError(null)} title="关闭提示">
                <X size={16} />
              </button>
            </div>
          ) : null}
          <form className="composer" onSubmit={sendMessage}>
            <textarea
              id="chat-composer"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
              placeholder={selected?.status === "active" ? "给她发一条消息..." : "恢复或新建会话后继续"}
              maxLength={12000}
              rows={1}
              disabled={!selected || selected.status !== "active" || streaming}
            />
            <button
              className={`send-command ${streaming ? "is-stopping" : ""}`}
              type={streaming ? "button" : "submit"}
              onClick={streaming ? stopStreaming : undefined}
              disabled={!streaming && (!draft.trim() || !selected || selected.status !== "active")}
              title={streaming ? "停止生成" : "发送消息"}
            >
              {streaming ? <Square size={17} fill="currentColor" /> : <Send size={19} />}
            </button>
          </form>
          <p className="composer-note">AI 生成内容可能有误，请以游戏原作设定为准。</p>
        </div>
      </section>

      {accountOpen ? (
        <AccountDialog
          name={account?.display_name || account?.username || "漂泊者"}
          onClose={() => setAccountOpen(false)}
          onChangePassword={changePassword}
          onLogout={async () => {
            abortRef.current?.abort();
            await logout();
          }}
        />
      ) : null}
    </main>
  );
}

function SessionRow({
  session,
  selected,
  renaming,
  menuOpen,
  disabled,
  onSelect,
  onBeginRename,
  onRename,
  onToggleMenu,
  onLifecycle,
}: {
  session: Session;
  selected: boolean;
  renaming: boolean;
  menuOpen: boolean;
  disabled: boolean;
  onSelect: () => void;
  onBeginRename: () => void;
  onRename: (title: string) => void;
  onToggleMenu: () => void;
  onLifecycle: (action: "end" | "resume" | "archive") => void;
}) {
  const [title, setTitle] = useState(session.title || "");
  useEffect(() => setTitle(session.title || ""), [session.title]);
  return (
    <div className={`session-row ${selected ? "is-selected" : ""}`}>
      {renaming ? (
        <form
          className="session-rename"
          onSubmit={(event) => {
            event.preventDefault();
            onRename(title);
          }}
        >
          <input
            autoFocus
            value={title}
            maxLength={120}
            onChange={(event) => setTitle(event.target.value)}
            aria-label="会话标题"
          />
          <button type="submit" className="icon-button" title="保存标题">
            <Check size={16} />
          </button>
        </form>
      ) : (
        <button className="session-select" type="button" onClick={onSelect} disabled={disabled}>
          <span>{session.title || fallbackSessionTitle(session)}</span>
          <small>{session.message_count} 条消息 · {relativeDate(session.updated_at)}</small>
        </button>
      )}
      {!renaming ? (
        <div className="session-actions">
          <button className="icon-button" type="button" onClick={onToggleMenu} title="会话操作" disabled={disabled}>
            <MoreHorizontal size={17} />
          </button>
          {menuOpen ? (
            <div className="session-menu">
              {session.status !== "archived" ? (
                <button type="button" onClick={onBeginRename}><Pencil size={15} />修改标题</button>
              ) : null}
              {session.status === "active" ? (
                <button type="button" onClick={() => onLifecycle("end")}><CircleStop size={15} />结束会话</button>
              ) : null}
              {session.status === "ended" ? (
                <button type="button" onClick={() => onLifecycle("resume")}><Play size={15} />恢复会话</button>
              ) : null}
              {session.status !== "archived" ? (
                <button type="button" onClick={() => onLifecycle("archive")}><Archive size={15} />归档会话</button>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function MessageBubble({ message }: { message: UiMessage }) {
  const assistant = message.role === "assistant";
  return (
    <article className={`message-row ${assistant ? "is-character" : "is-user"}`}>
      {assistant ? <img src={characterVisual.avatarUrl} alt="" className="message-avatar" /> : null}
      <div className={`message-bubble ${message.failed ? "is-failed" : ""}`}>
        {message.content || (message.pending ? <TypingIndicator /> : "回复未完成")}
      </div>
      <time>{formatTime(message.created_at)}</time>
    </article>
  );
}

function TypingIndicator() {
  return <span className="typing-indicator" aria-label="正在回复"><i /><i /><i /></span>;
}

function EmptyConversation({ onStart }: { onStart: () => void }) {
  return (
    <div className="empty-conversation">
      <img src={characterVisual.avatarUrl} alt="" />
      <h2>潮声刚刚安静下来</h2>
      <p>她似乎听见了你的脚步。现在，可以从一句问候开始。</p>
      <button type="button" onClick={onStart}>开始对话</button>
    </div>
  );
}

function AccountDialog({
  name,
  onClose,
  onChangePassword,
  onLogout,
}: {
  name: string;
  onClose: () => void;
  onChangePassword: (currentPassword: string, newPassword: string) => Promise<void>;
  onLogout: () => Promise<void>;
}) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [saved, setSaved] = useState(false);
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section className="account-dialog" role="dialog" aria-modal="true" aria-label="账号设置" onMouseDown={(event) => event.stopPropagation()}>
        <header>
          <div><h2>账号设置</h2><p>{name}</p></div>
          <button className="icon-button" onClick={onClose} title="关闭"><X size={18} /></button>
        </header>
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            setError(null);
            setSaved(false);
            try {
              await onChangePassword(current, next);
              setCurrent("");
              setNext("");
              setSaved(true);
            } catch (caught) {
              setError(caught);
            }
          }}
        >
          <label>当前密码<input type="password" autoComplete="current-password" value={current} onChange={(event) => setCurrent(event.target.value)} required /></label>
          <label>新密码<input type="password" autoComplete="new-password" value={next} onChange={(event) => setNext(event.target.value)} minLength={8} required /></label>
          <FormError error={error} />
          {saved ? <p className="success-message">密码已更新。</p> : null}
          <button className="secondary-command" type="submit">修改密码</button>
        </form>
        <button className="logout-command" type="button" onClick={onLogout}><LogOut size={17} />退出登录</button>
      </section>
    </div>
  );
}

function localMessage(
  id: string,
  sessionId: string,
  userId: string,
  role: string,
  content: string,
  createdAt: string,
): UiMessage {
  return {
    id,
    session_id: sessionId,
    user_id: userId,
    role,
    content,
    created_at: createdAt,
    emotion_label: null,
    importance_score: null,
  };
}

function sortMessages(messages: Message[]): Message[] {
  return [...messages].sort((a, b) => a.created_at.localeCompare(b.created_at));
}

function fallbackSessionTitle(session: Session): string {
  return `与${characterVisual.displayName}的对话`;
}

function statusText(status: Session["status"]): string {
  return { active: "进行中", ended: "已结束", archived: "已归档" }[status];
}

function relativeDate(value: string): string {
  const date = new Date(value);
  const today = new Date();
  if (date.toDateString() === today.toDateString()) {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(date);
  }
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(date);
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

export function handleStreamEventForTest(event: StreamEvent): string {
  return event.type;
}

type StreamOutcome = "none" | "completed" | "failed";

export function shouldClearErrorAfterStream(outcome: StreamOutcome): boolean {
  return outcome !== "failed";
}
