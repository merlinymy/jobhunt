import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { Toaster, toast } from "sonner";

import App from "./App";
import { ApiError } from "./api/client";
import "./index.css";

import ApplicationDetail from "./routes/ApplicationDetail";
import Fill from "./routes/Fill";
import NewApplication from "./routes/NewApplication";
import NotFound from "./routes/NotFound";
import Packet from "./routes/Packet";
import Pipeline from "./routes/Pipeline";
import Review from "./routes/Review";
import Stats from "./routes/Stats";
import Tailor from "./routes/Tailor";

function message(error: unknown): string {
  return error instanceof ApiError || error instanceof Error
    ? error.message
    : "Something went wrong.";
}

/* One listener at the cache level, reproducing the guarantee base.html had with
 * its single htmx:responseError handler. Per-mutation onError is the React
 * default and is exactly the thing you forget on the one button that matters.
 *
 * `meta.silent` opts the two form routes out: a validation error you have to act
 * on should be rendered next to the field, not vanish in eight seconds. */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
      // The point of this one: approve on the phone, then look at the laptop and
      // see it. Free, and better than anything HTMX did.
      refetchOnWindowFocus: true,
    },
  },
  queryCache: new QueryCache({
    onError: (error) => toast.error(message(error)),
  }),
  mutationCache: new MutationCache({
    onError: (error, _vars, _ctx, mutation) => {
      if (mutation.meta?.silent) return;
      toast.error(message(error));
    },
  }),
});

/* Paths mirror the Jinja routes exactly, so bookmarks and muscle memory survive. */
const router = createBrowserRouter([
  {
    element: <App />,
    children: [
      { path: "/", element: <Pipeline /> },
      { path: "/review", element: <Review /> },
      { path: "/applications/new", element: <NewApplication /> },
      { path: "/applications/:id", element: <ApplicationDetail /> },
      { path: "/packet/:id", element: <Packet /> },
      { path: "/tailor", element: <Tailor /> },
      { path: "/fill", element: <Fill /> },
      { path: "/stats", element: <Stats /> },
      { path: "*", element: <NotFound /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
      <Toaster position="top-center" richColors closeButton />
    </QueryClientProvider>
  </StrictMode>,
);
