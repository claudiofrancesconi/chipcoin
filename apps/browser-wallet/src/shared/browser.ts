type RuntimeLike = typeof chrome.runtime;
type StorageAreaLike = typeof chrome.storage.local;
type AlarmsLike = typeof chrome.alarms;

export function extensionRuntime(): RuntimeLike {
  const runtime = globalThis.chrome?.runtime ?? (globalThis as { browser?: { runtime: RuntimeLike } }).browser?.runtime;
  if (!runtime) {
    throw new Error("Extension runtime API is not available.");
  }
  return runtime;
}

export function extensionStorage(): StorageAreaLike {
  const storage = globalThis.chrome?.storage?.local
    ?? (globalThis as { browser?: { storage?: { local?: StorageAreaLike } } }).browser?.storage?.local;
  if (!storage) {
    throw new Error("Extension storage API is not available.");
  }
  return storage;
}

export function extensionAlarms(): AlarmsLike {
  const alarms = globalThis.chrome?.alarms ?? (globalThis as { browser?: { alarms: AlarmsLike } }).browser?.alarms;
  if (!alarms) {
    throw new Error("Extension alarms API is not available.");
  }
  return alarms;
}

export async function storageGet<T>(key: string): Promise<T | undefined> {
  const storage = extensionStorage();
  return new Promise<T | undefined>((resolve) => {
    storage.get(key, (result) => resolve(result[key] as T | undefined));
  });
}

export async function storageSet<T>(key: string, value: T): Promise<void> {
  const storage = extensionStorage();
  return new Promise<void>((resolve) => {
    storage.set({ [key]: value }, () => resolve());
  });
}

export async function storageRemove(key: string): Promise<void> {
  const storage = extensionStorage();
  return new Promise<void>((resolve) => {
    storage.remove(key, () => resolve());
  });
}
