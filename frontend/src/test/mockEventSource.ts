import { vi } from "vitest";

type MockListener = (event: Event) => void;

/**
 * Minimal EventSource double. Tests can emit named SSE frames with
 * `emit("done", payload)` and inspect `closed`.
 */
export class MockEventSource {
  static instances: MockEventSource[] = [];

  static reset(): void {
    MockEventSource.instances = [];
  }

  static latest(): MockEventSource {
    const instance = MockEventSource.instances[MockEventSource.instances.length - 1];
    if (!instance) throw new Error("没有创建 EventSource 实例");
    return instance;
  }

  readonly url: string;
  readonly withCredentials = false;
  readyState = 0;
  closed = false;
  onopen: ((this: EventSource, event: Event) => unknown) | null = null;
  onerror: ((this: EventSource, event: Event) => unknown) | null = null;
  onmessage: ((this: EventSource, event: MessageEvent) => unknown) | null = null;

  private readonly listeners = new Map<string, Set<MockListener>>();

  constructor(url: string | URL) {
    this.url = String(url);
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject | null): void {
    if (!listener) return;
    const handler: MockListener =
      typeof listener === "function"
        ? (listener as MockListener)
        : (event: Event) => listener.handleEvent(event);
    const set = this.listeners.get(type) ?? new Set<MockListener>();
    set.add(handler);
    this.listeners.set(type, set);
  }

  removeEventListener(type: string, listener: EventListenerOrEventListenerObject | null): void {
    if (!listener) return;
    const set = this.listeners.get(type);
    if (!set) return;
    set.forEach((handler) => {
      const matches =
        typeof listener === "function"
          ? handler === (listener as MockListener)
          : false;
      if (matches) set.delete(handler);
    });
  }

  close(): void {
    this.closed = true;
    this.readyState = 2;
  }

  emit(type: string, payload: unknown): void {
    const event = { data: JSON.stringify(payload), type } as unknown as MessageEvent<string>;
    this.dispatch(type, event);
  }

  emitOpen(): void {
    this.readyState = 1;
    this.dispatch("open", new Event("open"));
  }

  emitError(): void {
    this.dispatch("error", new Event("error"));
  }

  private dispatch(type: string, event: Event): void {
    const set = this.listeners.get(type);
    if (!set) return;
    set.forEach((handler) => handler(event));
  }
}

export function installEventSourceMock(): typeof MockEventSource {
  MockEventSource.reset();
  vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
  return MockEventSource;
}
