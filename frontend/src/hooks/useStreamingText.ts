import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Batch already-received provider deltas into animation frames.
 *
 * This is deliberately not a typewriter effect: callers append only text that
 * has arrived from the network. Frame batching prevents a high-frequency SSE
 * stream from forcing one React render per tiny transport chunk.
 */
export function useStreamingText(initialText = "") {
  const [text, setText] = useState(initialText);
  const pendingRef = useRef("");
  const frameRef = useRef<number | null>(null);
  const textRef = useRef(initialText);

  const flush = useCallback(() => {
    frameRef.current = null;
    const chunk = pendingRef.current;
    pendingRef.current = "";
    if (!chunk) return;
    setText(textRef.current);
  }, []);

  const append = useCallback(
    (delta: string) => {
      if (!delta) return;
      // 同步累加器立刻反映已收到的网络内容；同一动画帧连续抵达多个
      // delta 时，事件处理器读取 textRef 也不会覆盖前一个分块。
      textRef.current += delta;
      pendingRef.current += delta;
      if (frameRef.current == null) frameRef.current = window.requestAnimationFrame(flush);
    },
    [flush],
  );

  const replace = useCallback(
    (next: string) => {
      if (frameRef.current != null) {
        window.cancelAnimationFrame(frameRef.current);
        frameRef.current = null;
      }
      pendingRef.current = "";
      textRef.current = next;
      setText(next);
    },
    [],
  );

  /**
   * Reconcile an authoritative cumulative snapshot without turning each
   * provider delta into a full DOM replacement.  Native streaming endpoints
   * may send the final accumulated text after individual deltas; preserve the
   * append path whenever that snapshot extends the text already received.
   */
  const syncSnapshot = useCallback(
    (next: string) => {
      if (next === textRef.current) return;
      if (next.startsWith(textRef.current)) {
        append(next.slice(textRef.current.length));
        return;
      }
      replace(next);
    },
    [append, replace],
  );

  const clear = useCallback(() => replace(""), [replace]);

  const flushNow = useCallback(() => {
    if (frameRef.current != null) {
      window.cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    }
    const chunk = pendingRef.current;
    pendingRef.current = "";
    if (!chunk) return;
    setText(textRef.current);
  }, []);

  useEffect(
    () => () => {
      if (frameRef.current != null) window.cancelAnimationFrame(frameRef.current);
    },
    [],
  );

  return { text, textRef, append, replace, syncSnapshot, clear, flushNow };
}
