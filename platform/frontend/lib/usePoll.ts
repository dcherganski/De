"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";

/** GET `path` now and every `ms`; `refresh()` re-fetches at once (after a control action). */
export function usePoll<T>(path: string, ms: number, onUnauthorized?: () => void) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const unauthorized = useRef(onUnauthorized);
  unauthorized.current = onUnauthorized;
  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const d = await api.get<T>(path);
        if (alive) {
          setData(d);
          setError(null);
        }
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) unauthorized.current?.();
        else if (alive) setError(e instanceof Error ? e.message : "Грешка при зареждане.");
      }
    };
    load();
    const id = setInterval(load, ms);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [path, ms, tick]);

  return { data, error, refresh };
}
