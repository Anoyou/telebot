/** Incrementally decode newline-delimited JSON across arbitrary byte chunks. */
export class NdjsonDecoder<T> {
  private readonly decoder = new TextDecoder();
  private buffer = "";

  push(value?: Uint8Array): T[] {
    this.buffer += this.decoder.decode(value, { stream: true });
    return this.consumeCompleteLines();
  }

  finish(): T[] {
    this.buffer += this.decoder.decode();
    const values = this.consumeCompleteLines();
    const trailing = this.buffer.trim();
    this.buffer = "";
    if (trailing) values.push(JSON.parse(trailing) as T);
    return values;
  }

  private consumeCompleteLines(): T[] {
    const lines = this.buffer.split("\n");
    this.buffer = lines.pop() || "";
    const values: T[] = [];
    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed) values.push(JSON.parse(trimmed) as T);
    }
    return values;
  }
}
