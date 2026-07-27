"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            refetchOnWindowFocus: true,
            staleTime: 5_000,
            retry: 1,
            // The default "online" mode pauses a query when a fetch fails at
            // the network layer, leaving it pending-idle with no error to
            // render. Our API is on localhost, so browser connectivity says
            // nothing about whether it is reachable: keep firing and surface
            // the failure.
            networkMode: "always",
          },
        },
      }),
  );

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
