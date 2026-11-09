/**
 * React glue for the framework-free room client (client/src/room-client.ts). There is exactly ONE
 * event client per open room; React never opens a second socket. The client mutates its state in
 * place and calls onChange, so a counter is the snapshot React compares.
 */
import { useEffect, useMemo, useReducer, useRef, useSyncExternalStore } from "react";
import { browserDeps, RoomClient, type AiSignals, type DocumentSignals } from "threadforge-client";
import type { Api } from "./api";
import { AiStore } from "./ai/store";
import { DocumentStore } from "./documents/store";

export interface RoomHandles {
  client: RoomClient;
  store: DocumentStore;
  ai: AiStore;
}

export function useRoomHandles(api: Api, roomId: string, userId: string): RoomHandles {
  const handles = useMemo(() => {
    const store = new DocumentStore(api, roomId);
    const ai = new AiStore(api, roomId, userId);
    const client = new RoomClient(browserDeps(), {
      roomId,
      userId,
      httpBase: api.httpBase,
      wsBase: api.httpBase.replace(/^http/, "ws"),
      // runs BEFORE the client's cursor advances: if the store throws, the batch is retried
      onDocumentSignals: (signals: DocumentSignals) => store.onSignals(signals),
      // AI events carry ids and states only; the store refetches from the authorized endpoints
      onAiSignals: (signals: AiSignals) => ai.applySignals(signals),
    });
    return { client, store, ai };
  }, [api, roomId, userId]);

  useEffect(() => {
    handles.store.activate();
    handles.client.start();
    void handles.store.loadList();
    handles.ai.activate();
    void handles.ai.load();
    return () => {
      handles.client.stop(true);
      handles.store.dispose();
      handles.ai.dispose();
    };
  }, [handles]);
  return handles;
}

/** Re-render whenever the room client reports a change; returns its (mutable) state. */
export function useClientState(client: RoomClient): RoomClient["state"] {
  const version = useRef(0);
  const subscribe = useMemo(
    () => (notify: () => void) =>
      client.onChange(() => {
        version.current++;
        notify();
      }),
    [client],
  );
  useSyncExternalStore(subscribe, () => version.current);
  return client.state;
}

export function useStore(store: DocumentStore) {
  return useSyncExternalStore(store.subscribe, store.getSnapshot);
}

export function useForceUpdate(): () => void {
  const [, bump] = useReducer((n: number) => n + 1, 0);
  return bump;
}

export function useAi(ai: AiStore) {
  return useSyncExternalStore(ai.subscribe, ai.getSnapshot);
}
