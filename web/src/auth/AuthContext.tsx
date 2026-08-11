import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { Navigate, useLocation } from "react-router-dom";

import { api, ApiError, subscribeAuthInvalidation } from "../api/client";
import type { Account } from "../api/types";

type AuthContextValue = {
  account: Account | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  acceptInvitation: (token: string, password: string) => Promise<void>;
  acceptPasswordReset: (token: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  changePassword: (currentPassword: string, newPassword: string) => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: PropsWithChildren) {
  const [account, setAccount] = useState<Account | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(
    () =>
      subscribeAuthInvalidation(() => {
        setAccount(null);
      }),
    [],
  );

  useEffect(() => {
    let active = true;
    api
      .me()
      .then((value) => active && setAccount(value))
      .catch((error: unknown) => {
        if (!(error instanceof ApiError && (error.status === 401 || error.status === 403))) {
          console.warn("Unable to restore the browser session.");
        }
      })
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    setAccount(await api.login(username, password));
  }, []);

  const acceptInvitation = useCallback(async (token: string, password: string) => {
    setAccount(await api.acceptInvitation(token, password));
  }, []);

  const acceptPasswordReset = useCallback(async (token: string, password: string) => {
    setAccount(await api.acceptPasswordReset(token, password));
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      setAccount(null);
    }
  }, []);

  const changePassword = useCallback(async (currentPassword: string, newPassword: string) => {
    await api.changePassword(currentPassword, newPassword);
    setAccount(await api.me());
  }, []);

  const value = useMemo(
    () => ({
      account,
      loading,
      login,
      acceptInvitation,
      acceptPasswordReset,
      logout,
      changePassword,
    }),
    [
      account,
      loading,
      login,
      acceptInvitation,
      acceptPasswordReset,
      logout,
      changePassword,
    ],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used within AuthProvider");
  return value;
}

export function ProtectedRoute({ children }: PropsWithChildren) {
  const { account, loading } = useAuth();
  const location = useLocation();
  if (loading) return <div className="app-loading">正在连接小屋...</div>;
  if (!account) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return children;
}
