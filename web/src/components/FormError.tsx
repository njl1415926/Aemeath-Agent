import { AlertCircle } from "lucide-react";

import { ApiError } from "../api/client";

export function FormError({ error }: { error: unknown }) {
  if (!error) return null;
  const message = error instanceof Error ? error.message : "操作失败，请稍后重试。";
  const requestId = error instanceof ApiError ? error.requestId : null;
  return (
    <div className="form-error" role="alert">
      <AlertCircle size={16} aria-hidden="true" />
      <span>
        {message}
        {requestId ? <small>请求编号：{requestId}</small> : null}
      </span>
    </div>
  );
}
