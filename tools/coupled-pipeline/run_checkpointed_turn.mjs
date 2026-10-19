function errorText(error) {
  return String(error?.message || error || "unknown resume error").slice(0, 1000);
}

/**
 * Run one SDK turn. If the saved remote thread cannot be opened and no event has
 * arrived, invalidate only the thread binding and continue from durable run
 * artifacts in a fresh thread. Once any event arrives, never retry implicitly:
 * the worker may already have caused side effects and the supervisor must decide.
 */
export async function runCheckpointedTurn({
  codex,
  checkpoint,
  threadOptions,
  prompt,
  output,
  signal,
  onEvent,
}) {
  let shouldResume = Boolean(checkpoint.threadId);
  let strategy = shouldResume ? "thread" : "fresh";
  let fallbackError = "";

  for (let attempt = 0; attempt < 2; attempt += 1) {
    let observedEvents = 0;
    try {
      const thread = shouldResume
        ? codex.resumeThread(checkpoint.threadId, threadOptions)
        : codex.startThread(threadOptions);
      const streamed = await thread.runStreamed(prompt, { signal });
      for await (const event of streamed.events) {
        observedEvents += 1;
        if (event.type === "thread.started") await checkpoint.save(event.thread_id, output);
        await onEvent(event);
      }
      return { strategy, fallbackError };
    } catch (error) {
      if (shouldResume && observedEvents === 0 && attempt === 0 && error?.name !== "AbortError") {
        fallbackError = errorText(error);
        await checkpoint.invalidateThread(fallbackError);
        shouldResume = false;
        strategy = "fresh_from_handoff";
        continue;
      }
      const failure = error instanceof Error ? error : new Error(errorText(error));
      failure.resumeStrategy = strategy;
      failure.resumeFallbackError = fallbackError;
      throw failure;
    }
  }
  throw new Error("checkpoint turn exhausted without a result");
}
