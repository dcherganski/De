"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, ApiError } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post("/api/auth/login", { username, password });
      router.replace("/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Неуспешен вход.");
    } finally {
      setBusy(false);
      setPassword("");
    }
  }

  return (
    <main className="wrap">
      <div className="card login">
        <h1>Вход</h1>
        <form onSubmit={submit}>
          <label>
            Потребител
            <input name="username" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required />
          </label>
          <label>
            Парола
            <input name="password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
          </label>
          {error && <p className="notice error" role="alert">{error}</p>}
          <button className="primary" type="submit" disabled={busy}>Влез</button>
          <p><small>Потребителят се създава на сървъра с <span className="mono">python -m app.cli create-user</span>.</small></p>
        </form>
      </div>
    </main>
  );
}
