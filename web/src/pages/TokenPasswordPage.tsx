import { useEffect, useState, type FormEvent } from "react";
import { ArrowRight, KeyRound } from "lucide-react";
import { Navigate, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { AuthShell } from "../components/AuthShell";
import { FormError } from "../components/FormError";

export function TokenPasswordPage({ mode }: { mode: "invite" | "reset" }) {
  const { account, acceptInvitation, acceptPasswordReset } = useAuth();
  const navigate = useNavigate();
  const [token] = useState(() => readFragmentToken(window.location.hash));
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [submitting, setSubmitting] = useState(false);
  const invite = mode === "invite";

  useEffect(() => {
    clearSensitiveFragment();
  }, []);

  if (account) return <Navigate to="/chat" replace />;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!token) {
      setError(new Error("链接中缺少有效凭证，请使用完整邀请链接。"));
      return;
    }
    if (password !== confirm) {
      setError(new Error("两次输入的密码不一致。"));
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      if (invite) await acceptInvitation(token, password);
      else await acceptPasswordReset(token, password);
      navigate("/chat", { replace: true });
    } catch (caught) {
      setError(caught);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <AuthShell
      title={invite ? "接受邀请" : "重设密码"}
      subtitle={invite ? "设置密码后即可开启专属通讯" : "为账号设置一枚新的通行密钥"}
    >
      <form className="auth-form" onSubmit={submit}>
        <PasswordField
          label="新密码"
          value={password}
          onChange={setPassword}
          autoComplete="new-password"
        />
        <PasswordField
          label="确认密码"
          value={confirm}
          onChange={setConfirm}
          autoComplete="new-password"
        />
        <FormError error={error} />
        <button className="primary-command" type="submit" disabled={submitting || !token}>
          <span>{submitting ? "正在确认..." : invite ? "接受邀请" : "完成重置"}</span>
          <ArrowRight size={18} aria-hidden="true" />
        </button>
      </form>
    </AuthShell>
  );
}

function PasswordField({
  label,
  value,
  onChange,
  autoComplete,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  autoComplete: string;
}) {
  return (
    <label>
      <span>{label}</span>
      <span className="field-control">
        <KeyRound size={17} aria-hidden="true" />
        <input
          type="password"
          autoComplete={autoComplete}
          minLength={8}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          required
        />
      </span>
    </label>
  );
}

export function readFragmentToken(hash: string): string | null {
  const value = hash.startsWith("#") ? hash.slice(1) : hash;
  if (!value) return null;
  const params = new URLSearchParams(value);
  return params.get("token") || (value.includes("=") ? null : value);
}

export function clearSensitiveFragment(): void {
  if (!window.location.hash) return;
  window.history.replaceState(
    window.history.state,
    "",
    `${window.location.pathname}${window.location.search}`,
  );
}
