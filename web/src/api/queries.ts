import { useQuery, type UseQueryOptions } from "@tanstack/react-query";
import { api, qs } from "./client";
import { k, type Params } from "./keys";
import type {
  Detail, Fill, Meta, Packet, Pipeline, ReviewBatch, Stats, UrlCheck, Contact,
} from "./types";

/** Constants and profile data. Neither changes without a deploy or a
 *  `make load-profile`, so refetching them on focus is pure noise. */
const STATIC = { staleTime: Infinity } satisfies Partial<UseQueryOptions>;

export function useMeta() {
  return useQuery({ queryKey: k.meta(), queryFn: () => api.get<Meta>("/meta"), ...STATIC });
}

export function usePipeline(params: Params) {
  return useQuery({
    queryKey: k.pipeline(params),
    queryFn: () => api.get<Pipeline>(`/pipeline${qs(params)}`),
    placeholderData: (previous) => previous, // filtering should not blank the table
  });
}

export function useApplication(id: number) {
  return useQuery({
    queryKey: k.application(id),
    queryFn: () => api.get<Detail>(`/applications/${id}`),
  });
}

export function useReview(limit: number) {
  return useQuery({
    queryKey: k.review(limit),
    queryFn: () => api.get<ReviewBatch>(`/review${qs({ limit })}`),
  });
}

export function usePacket(id: number) {
  return useQuery({ queryKey: k.packet(id), queryFn: () => api.get<Packet>(`/packet/${id}`) });
}

export function useStats(params: Params) {
  return useQuery({
    queryKey: k.stats(params),
    queryFn: () => api.get<Stats>(`/stats${qs(params)}`),
    placeholderData: (previous) => previous,
  });
}

export function useFill() {
  return useQuery({ queryKey: k.fill(), queryFn: () => api.get<Fill>("/fill"), ...STATIC });
}

export function useContacts() {
  return useQuery({
    queryKey: k.contacts(),
    queryFn: () => api.get<{ contacts: Contact[] }>("/contacts"),
    ...STATIC,
  });
}

/** Live duplicate check on the entry form.
 *
 *  HTMX debounced this with `keyup changed delay:600ms`. Here the debounce is in
 *  the caller and TanStack handles caching and cancellation, which is strictly
 *  better: retyping a URL you already checked costs nothing. */
export function useUrlCheck(url: string) {
  return useQuery({
    queryKey: k.urlCheck(url),
    queryFn: () => api.get<UrlCheck>(`/applications/check-url${qs({ apply_url: url })}`),
    enabled: url.trim().length > 0,
    staleTime: 30_000,
  });
}
