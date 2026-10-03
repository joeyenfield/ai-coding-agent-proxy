import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api";

export function useStatus() {
  return useQuery({ queryKey: ["status"], queryFn: api.status, refetchInterval: 2000 });
}

export function useSessions() {
  return useQuery({ queryKey: ["sessions"], queryFn: api.sessions, refetchInterval: 4000 });
}

/** Full request history, refetched only when the proxy reports new or in-flight requests. */
export function useRequests() {
  const status = useStatus();
  const revision = `${status.data?.total_requests ?? 0}:${status.data?.current_requests.length ?? 0}`;
  return useQuery({ queryKey: ["requests", revision], queryFn: api.requests, placeholderData: (previous) => previous });
}

export function useModels() {
  return useQuery({ queryKey: ["models"], queryFn: api.models, refetchInterval: 15000 });
}

export function useAgents() {
  return useQuery({ queryKey: ["agents"], queryFn: api.agents, staleTime: 30000 });
}

export type ThemeChoice = "system" | "light" | "dark";

export function useTheme(): [ThemeChoice, (value: ThemeChoice) => void] {
  const [choice, setChoice] = useState<ThemeChoice>(() => {
    try {
      return (localStorage.getItem("ai-proxy-theme") as ThemeChoice) || "system";
    } catch {
      return "system";
    }
  });
  useEffect(() => {
    const media = matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      document.documentElement.dataset.theme = choice === "system" ? (media.matches ? "dark" : "light") : choice;
    };
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [choice]);
  const update = useCallback((value: ThemeChoice) => {
    setChoice(value);
    try {
      localStorage.setItem("ai-proxy-theme", value);
    } catch {
      // Storage may be unavailable in private windows.
    }
  }, []);
  return [choice, update];
}

const ToastContext = createContext<(message: string) => void>(() => undefined);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState<string | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const notify = useCallback((value: string) => {
    setMessage(value);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setMessage(null), 4000);
  }, []);
  return (
    <ToastContext.Provider value={notify}>
      {children}
      <div className="toast" role="status" aria-live="polite" hidden={!message}>
        {message}
      </div>
    </ToastContext.Provider>
  );
}

export const useToast = () => useContext(ToastContext);

export function useClipboard() {
  const notify = useToast();
  return useCallback(
    async (text: string, label = "Copied") => {
      try {
        await navigator.clipboard.writeText(text);
        notify(label);
      } catch {
        notify("Clipboard unavailable. Select the text and copy it manually.");
      }
    },
    [notify],
  );
}

export function download(content: string, filename: string) {
  const url = URL.createObjectURL(new Blob([content], { type: "application/json;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
