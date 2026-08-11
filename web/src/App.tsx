import { Navigate, Route, Routes } from "react-router-dom";

import { AuthProvider, ProtectedRoute } from "./auth/AuthContext";
import { ChatPage } from "./pages/ChatPage";
import { LoginPage } from "./pages/LoginPage";
import { TokenPasswordPage } from "./pages/TokenPasswordPage";

export function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/invite" element={<TokenPasswordPage mode="invite" />} />
        <Route path="/reset-password" element={<TokenPasswordPage mode="reset" />} />
        <Route
          path="/chat"
          element={
            <ProtectedRoute>
              <ChatPage />
            </ProtectedRoute>
          }
        />
        <Route path="/" element={<Navigate to="/chat" replace />} />
        <Route path="*" element={<Navigate to="/chat" replace />} />
      </Routes>
    </AuthProvider>
  );
}
