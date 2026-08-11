import type { PropsWithChildren, ReactNode } from "react";
import { Sparkles } from "lucide-react";

import { characterVisual } from "../config/character";

type Props = PropsWithChildren<{
  title: string;
  subtitle: string;
  footer?: ReactNode;
}>;

export function AuthShell({ title, subtitle, footer, children }: Props) {
  return (
    <main className="auth-page">
      <section className="auth-intro" aria-label="角色介绍">
        <div className="auth-brand">
          <Sparkles size={18} aria-hidden="true" />
          <span>Aemeath Agent</span>
        </div>
        <div className="auth-intro-copy">
          <p className="auth-kicker">专属通讯已建立</p>
          <h1>{characterVisual.displayName}</h1>
          <p>跨过潮声与记忆，在这里继续属于漂泊者的对话。</p>
        </div>
      </section>
      <section className="auth-panel">
        <div className="auth-panel-heading">
          <img src={characterVisual.avatarUrl} alt="" className="auth-avatar" />
          <div>
            <h2>{title}</h2>
            <p>{subtitle}</p>
          </div>
        </div>
        {children}
        {footer ? <div className="auth-footer">{footer}</div> : null}
      </section>
    </main>
  );
}
