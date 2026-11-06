// Recordings made without signal wait in IndexedDB until the phone is back online.
export interface PendingClip {
  id: string;
  projectId: string;
  setupId: string;
  blob: Blob;
  fileName: string;
  createdAt: number;
}

const DB = "reelcraft";
const STORE = "pending";

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE, { keyPath: "id" });
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function tx<T>(mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await open();
  return new Promise((resolve, reject) => {
    const req = fn(db.transaction(STORE, mode).objectStore(STORE));
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

export const savePending = (c: PendingClip) => tx("readwrite", (s) => s.put(c));
export const removePending = (id: string) => tx("readwrite", (s) => s.delete(id));
export async function listPending(projectId: string): Promise<PendingClip[]> {
  const all = (await tx("readonly", (s) => s.getAll())) as PendingClip[];
  return all.filter((c) => c.projectId === projectId).sort((a, b) => a.createdAt - b.createdAt);
}
