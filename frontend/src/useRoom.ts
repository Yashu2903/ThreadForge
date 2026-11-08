/**
 * React glue for the framework-free room client (client/src/room-client.ts). There is exactly ONE
 * event client per open room; React never opens a second socket. The client mutates its state in
 * place and calls onChange, so a counter is the snapshot React compares.
 */
import { useEffect, useMemo, useReducer, useRef, useSyncExternalStore } from "react";
import { browserDeps, RoomClient, type DocumentSignals } from "threadforge-client";
import type { Api } from "./api";
import { DocumentStore } from "./documents/store";

export interface RoomHandles {
  client: RoomClient;
  store: DocumentStore;
}

export function useRoomHandles(api: Api, roomId: string, userId: string): RoomHandles {
  const handles = useMemo(() => {
    const store = new DocumentStore(api, roomId);
    const client = new RoomClient(browserDeps(), {
      roomId,
      userId,
      httpBase: api.httpBase,
      wsBase: api.httpBase.replace(/^http/, "ws"),
      // runs BEFORE the client's cursor advances: if the store throws, the batch is retried
      onDocumentSignals: (signals: DocumentSignals) => store.onSignals(signals),
    });
    return { client, store };
  }, [api, roomId, userId]);

  useEffect(() => {
    handles.store.activate();
    handles.client.start();
    void handles.store.loadList();
    return () => {
      handles.client.stop(true);
      handles.store.dispose();
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
