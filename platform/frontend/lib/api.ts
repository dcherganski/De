// Fetch helpers. Every mutating call carries the CSRF header the API requires.
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public details?: string[],
  ) {
    super(message);
  }
}

function messageOf(body: unknown, status: number): { message: string; details?: string[] } {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return { message: detail };
    if (detail && typeof detail === "object" && "message" in detail) {
      const d = detail as { message: string; reasons?: string[] };
      return { message: d.message, details: d.reasons };
    }
  }
  return { message: `Грешка ${status}` };
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "X-Requested-With": "platform" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
    cache: "no-store",
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const { message, details } = messageOf(data, res.status);
    throw new ApiError(res.status, message, details);
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body: unknown = {}) => request<T>("POST", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
};
