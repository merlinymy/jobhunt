import { useMutation, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ApiError, api } from "./client";
import { k } from "./keys";
import type {
  ChatMessage, Decision, Detail, Packet, PromptDetail, ReviewBatch, Run, RunPipeline,
  Runs, TailorResult,
} from "./types";

/** Everything a decision can move.
 *
 *  Four mutations invalidate exactly this set, and defining it once is what
 *  stops the bug where approving from /review leaves the pipeline counts and
 *  the stats page showing yesterday's numbers.
 */
function invalidatePipeline(qc: QueryClient) {
  void qc.invalidateQueries({ queryKey: ["pipeline"] });
  void qc.invalidateQueries({ queryKey: ["applications"] });
  void qc.invalidateQueries({ queryKey: ["stats"] });
}

/** Approve or skip one card.
 *
 *  Optimistic, and the card is marked in place rather than removed. That copies
 *  a deliberate choice from the template this replaces: a decision stays visible
 *  so a mis-click is obvious immediately, and a stable array keeps the scroll
 *  position where it was — which on a phone is the difference between a queue
 *  you can work and one that jumps under your thumb.
 */
export function useDecide(limit: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, outcome }: { id: number; outcome: "approve" | "skip" }) =>
      api.post<Decision>(`/review/${id}/${outcome}`),
    onMutate: async ({ id, outcome }) => {
      await qc.cancelQueries({ queryKey: k.review(limit) });
      const previous = qc.getQueryData<ReviewBatch>(k.review(limit));
      qc.setQueryData<ReviewBatch>(k.review(limit), (old) =>
        old
          ? {
              ...old,
              batch: old.batch.map((card) =>
                card.application_id === id
                  ? { ...card, pending: true, decided: outcome === "approve" ? "approved" : "skipped" }
                  : card,
              ),
            }
          : old,
      );
      return { previous };
    },
    onError: (error, _vars, context) => {
      // 409 means it really was decided elsewhere — another tab, or a
      // double-tap. Rolling back to "undecided" would be the lie.
      const conflict = error instanceof Error && "status" in error && error.status === 409;
      if (!conflict && context?.previous) {
        qc.setQueryData(k.review(limit), context.previous);
      }
    },
    onSettled: (data) => {
      void qc.invalidateQueries({ queryKey: ["review"] });
      invalidatePipeline(qc);
      if (data?.application_id) {
        void qc.invalidateQueries({ queryKey: k.application(data.application_id) });
      }
    },
  });
}

export function useTransition(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { to_state: string; detail?: string }) =>
      api.post<Detail>(`/applications/${id}/transition`, body),
    onSuccess: (data) => {
      qc.setQueryData(k.application(id), data);
      void qc.invalidateQueries({ queryKey: k.packet(id) });
      invalidatePipeline(qc);
    },
  });
}

export function useAddNote(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (detail: string) => api.post<Detail>(`/applications/${id}/note`, { detail }),
    onSuccess: (data) => qc.setQueryData(k.application(id), data),
  });
}

export function useHonesty(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (value: number) =>
      api.post<Detail>(`/applications/${id}/honesty`, { would_apply_anyway: value }),
    onSuccess: (data) => {
      qc.setQueryData(k.application(id), data);
      // The ratio is on the pipeline header and the stats page both.
      invalidatePipeline(qc);
    },
  });
}

/** Build a packet. Never optimistic: it is a real model call that costs money,
 *  takes ~20s, and can legitimately come back refusing to write a resume. */
export function useBuildPacket(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<Packet>(`/packet/${id}/build`),
    onSuccess: (data) => {
      qc.setQueryData(k.packet(id), data);
      void qc.invalidateQueries({ queryKey: k.application(id) });
      invalidatePipeline(qc);
    },
  });
}

/** One conversational turn about the resume.
 *
 *  Synchronous, unlike a build: one model call and no render, so the reply comes
 *  back with the response. The whole packet is refetched rather than just the
 *  thread — a turn can be answered from a resume that was revised in another
 *  tab, and the findings shown beside it have to agree with it. */
export function useSendChat(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (message: string) =>
      api.post<{ messages: ChatMessage[] }>(`/packet/${id}/chat`, { message }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: k.packet(id) });
    },
    /* Inline, not a toast: a rejected message is one you have to retype, and a
       notice that disappears in eight seconds is the wrong place to say so. */
    meta: { silent: true },
  });
}

/** Put a proposed revision on the row. Renders, so it takes a few seconds. */
export function useApplyProposal(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (messageId: number) =>
      api.post<Packet>(`/packet/${id}/chat/${messageId}/apply`),
    onSuccess: (data) => {
      qc.setQueryData(k.packet(id), data);
      void qc.invalidateQueries({ queryKey: k.application(id) });
      invalidatePipeline(qc);
    },
  });
}

/** Draft answers for every box on the form, five options each. */
export function useDraftFormAnswers(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (pasted: string) =>
      api.post<Packet>(`/packet/${id}/form-answers`, { pasted }),
    onSuccess: (data) => qc.setQueryData(k.packet(id), data),
    /* Inline: a rejected paste is one you have to fix, and a toast that clears
       in eight seconds is the wrong place to say what was wrong with it. */
    meta: { silent: true },
  });
}

/** Pick one. A drafted answer is also written to the bank for this company. */
export function useChooseFormAnswer(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ index, option }: { index: number; option: number }) =>
      api.post<Packet>(`/packet/${id}/form-answers/${index}/choose/${option}`),
    onSuccess: (data) => qc.setQueryData(k.packet(id), data),
  });
}

export function useGenerateAnswer(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (key: string) => api.post<Pick<Packet, "answers" | "unknowns">>(
      `/packet/${id}/answers/${encodeURIComponent(key)}/generate`,
    ),
    onSuccess: (data) =>
      qc.setQueryData<Packet>(k.packet(id), (old) => (old ? { ...old, ...data } : old)),
  });
}

export function useSetAnswer(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ key, answer }: { key: string; answer: string }) =>
      api.post<Pick<Packet, "answers" | "unknowns">>(
        `/packet/${id}/answers/${encodeURIComponent(key)}/set`,
        { answer },
      ),
    onSuccess: (data) =>
      qc.setQueryData<Packet>(k.packet(id), (old) => (old ? { ...old, ...data } : old)),
  });
}

/** The two form routes render their own errors, so they opt out of the global
 *  toast: a validation message you have to act on should not disappear in
 *  eight seconds while you are still reading the form. */
const INLINE = { meta: { silent: true } };

export function useCreateApplication() {
  const qc = useQueryClient();
  return useMutation({
    ...INLINE,
    mutationFn: (form: Record<string, unknown>) => api.post<{ id: number }>("/applications", form),
    onSuccess: () => invalidatePipeline(qc),
  });
}

/** Start a discovery or scoring run.
 *
 *  Never optimistic and never retried. The server claims the lock inside the
 *  request, so the 202 means "it is running" and a 409 means something else got
 *  there first — a second tab, the phone, or the 06:30 agent. That is not an
 *  error worth a red toast: the thing the click wanted is happening, so say so
 *  quietly and let the progress panel show whose run it actually is.
 */
export function useStartRun() {
  const qc = useQueryClient();
  return useMutation({
    ...INLINE,
    retry: false,
    mutationFn: (pipeline: RunPipeline) => api.post<Run>("/runs", { pipeline }),
    // Seed the run the 202 just described, rather than waiting for the refetch
    // below to confirm it. Otherwise there is a gap of one round trip where the
    // mutation is no longer pending and no run is known yet — during which the
    // button re-enables itself and invites the second click.
    onSuccess: (run) => {
      qc.setQueryData<Runs>(k.runs(), (old) => (old ? { ...old, active: run } : old));
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        toast(error.message, { description: "Following it below." });
        return;
      }
      toast.error(error.message);
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: k.runs() }),
  });
}

/** Prompt edits.
 *
 *  All three write the same detail payload back into the cache and invalidate
 *  the index, whose per-task dot shows whether a task is on the file or on an
 *  override. Inline errors: "that is 12 characters" is a message to act on, not
 *  one to watch disappear in eight seconds. */
function usePromptMutation<T>(task: string, fn: (input: T) => Promise<PromptDetail>) {
  const qc = useQueryClient();
  return useMutation({
    ...INLINE,
    mutationFn: fn,
    onSuccess: (data) => {
      qc.setQueryData(k.prompt(task), data);
      void qc.invalidateQueries({ queryKey: k.prompts() });
    },
  });
}

export function useSavePrompt(task: string) {
  return usePromptMutation(task, (body: { body: string; note: string }) =>
    api.put<PromptDetail>(`/prompts/${task}`, body),
  );
}

export function useRevertPrompt(task: string) {
  // `void` rather than no parameter at all: TanStack derives the mutate()
  // signature from this, and a zero-arg function makes mutate() require an
  // argument it has no use for.
  return usePromptMutation<void>(task, () =>
    api.post<PromptDetail>(`/prompts/${task}/revert`),
  );
}

export function useActivatePrompt(task: string) {
  return usePromptMutation(task, (sha: string) =>
    api.post<PromptDetail>(`/prompts/${task}/activate/${sha}`),
  );
}

export function useTailor() {
  return useMutation({
    ...INLINE,
    mutationFn: (body: { jd_text: string; limit?: number }) =>
      api.post<TailorResult>("/tailor", body),
  });
}

export { toast };
