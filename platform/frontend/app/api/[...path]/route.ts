// Same-origin proxy to the FastAPI backend. Only the headers the API needs are forwarded.
import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";

const API_URL = process.env.API_URL ?? "http://127.0.0.1:8000";
const REQUEST_HEADERS = ["cookie", "content-type", "x-requested-with"];
const RESPONSE_HEADERS = ["content-type", "cache-control"];

type Ctx = { params: Promise<{ path: string[] }> };

async function proxy(req: NextRequest, ctx: Ctx): Promise<Response> {
  const { path } = await ctx.params;
  if (path.some((p) => p === "." || p === ".." || p === "")) {
    return Response.json({ detail: "Невалиден път." }, { status: 400 });
  }
  const url = new URL(`/api/${path.map(encodeURIComponent).join("/")}`, API_URL);
  url.search = req.nextUrl.search;
  const headers = new Headers();
  for (const name of REQUEST_HEADERS) {
    const value = req.headers.get(name);
    if (value) headers.set(name, value);
  }
  const init: RequestInit = { method: req.method, headers, redirect: "manual", cache: "no-store" };
  if (req.method !== "GET" && req.method !== "HEAD") init.body = await req.arrayBuffer();
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch {
    return Response.json({ detail: "API сървърът не отговаря." }, { status: 502 });
  }
  const out = new Headers();
  for (const name of RESPONSE_HEADERS) {
    const value = res.headers.get(name);
    if (value) out.set(name, value);
  }
  for (const cookie of res.headers.getSetCookie()) out.append("set-cookie", cookie);
  return new Response(res.body, { status: res.status, headers: out });
}

export { proxy as GET, proxy as POST, proxy as DELETE };
