import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "@fontsource-variable/inter";
import "@fontsource/jetbrains-mono/400.css";
import "./styles.css";
import { App } from "./App";
import { ToastProvider } from "./hooks";
import { RestartProvider } from "./components/Restart";
import { ApiError } from "./api";

const queryClient: QueryClient = new QueryClient({
  defaultOptions: { queries: { retry: (count, error) => !(error instanceof ApiError && error.status === 401) && count < 1, refetchOnWindowFocus: true } },
  // A request refused for want of a sign-in (it expired, or the account went): check again, which shows sign-in.
  queryCache: new QueryCache({
    onError: (error) => {
      if (error instanceof ApiError && error.status === 401) queryClient.invalidateQueries({ queryKey: ["auth"] });
    },
  }),
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <ToastProvider>
          <RestartProvider>
            <App />
          </RestartProvider>
        </ToastProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
