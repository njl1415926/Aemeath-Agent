import { useState, type FormEvent } from "react";
import { ArrowRight, KeyRound, UserRound } from "lucide-react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { AuthShell } from "../components/AuthShell";
import { FormError } from "../components/FormError";

export function LoginPage() {
  const { account, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [submitting, setSubmitting] = useState(false);

  if (account) return <Navigate to="/chat" replace />;

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(username.trim(), password);
      const from = (location.state as { from?: string } | null)?.from;
      navigate(from || "/chat", { replace: true });
    } catch (caught) {
      setError(caught);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <AuthShell
      title="欢迎回来"
      subtitle="使用邀请账号进入专属对话"
      footer={<span>当前为邀请制测试，仅已获邀请的漂泊者可以登录。</span>}
    >
      <form className="auth-form" onSubmit={submit} data-testid="login-form">
        <label>
          <span>用户名</span>
          <span className="field-control">
            <UserRound size={17} aria-hidden="true" />
            <input
              data-testid="login-username"
              autoComplete="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              maxLength={64}
              required
            />
          </span>
        </label>
        <label>
          <span>密码</span>
          <span className="field-control">
            <KeyRound size={17} aria-hidden="true" />
            <input
              data-testid="login-password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </span>
        </label>
        <FormError error={error} />
        <button
          className="primary-command"
          type="submit"
          disabled={submitting}
          data-testid="login-submit"
        >
          <span>{submitting ? "正在连接..." : "进入小屋"}</span>
          <ArrowRight size={18} aria-hidden="true" />
        </button>
      </form>
    </AuthShell>
  );
}
