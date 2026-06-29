/**
 * Typed fetch wrappers for all music-sync API endpoints.
 * Interfaces mirror the Pydantic schemas in musicsync/web/app.py exactly.
 */

// ---------------------------------------------------------------------------
// Response types
// ---------------------------------------------------------------------------

export interface TrackLinks {
  spotify: string | null;
  apple: string;
  tidal: string | null;
}

export interface TrackPresence {
  spotify: boolean;
  apple: boolean;
  tidal: boolean;
}

export interface LibraryTrack {
  key: string;
  name: string;
  artist: string;
  presence: TrackPresence;
  links: TrackLinks;
  on_all_three: boolean;
  album: string | null;
  artwork_url: string | null;
  duration_sec: number | null;
  year: string | null;
  /** Per-platform ISO date string when the track was added, or null if unknown. */
  added_at: Record<string, string | null>;
}

export interface LibraryPage {
  items: LibraryTrack[];
  total: number;
  page: number;
  page_size: number;
}

export interface AuthStatus {
  platform: string;
  configured: boolean;
  connected: boolean;
}

export interface ConnectResult {
  started: boolean;
  message: string;
}

export interface SyncRequest {
  offline?: boolean;
}

export interface SyncTrack {
  key: string;
  name: string;
  artist: string;
}

export interface SyncDiff {
  to_sync: Record<string, SyncTrack[]>;
  counts: Record<string, number>;
}

export interface ApplyResult {
  platform: string;
  applied: number;
}

// ---------------------------------------------------------------------------
// Platform metadata — single source of truth for the three views
// ---------------------------------------------------------------------------

export const PLATFORM_META = [
  { id: "spotify", label: "Spotify", emoji: "🟢" },
  { id: "apple", label: "Apple Music", emoji: "🍎" },
  { id: "tidal", label: "Tidal", emoji: "🌊" },
] as const;

export type Platform = (typeof PLATFORM_META)[number]["id"];

/** Just the platform ids, for views that only need the id list. */
export const PLATFORM_IDS: readonly Platform[] = PLATFORM_META.map((p) => p.id);

// ---------------------------------------------------------------------------
// Query helpers
// ---------------------------------------------------------------------------

export interface LibraryQuery {
  q?: string;
  filter?: string;
  sort_by?: string;
  page?: number;
  page_size?: number;
}

// ---------------------------------------------------------------------------
// Fetch wrappers
// ---------------------------------------------------------------------------

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  // State-changing requests carry X-Requested-With to force a CORS preflight,
  // preventing cross-origin drive-by POSTs to the local API. GETs don't need it.
  const finalInit: RequestInit | undefined =
    method === "GET"
      ? init
      : {
          ...init,
          headers: {
            "X-Requested-With": "XMLHttpRequest",
            ...(init?.headers ?? {}),
          },
        };
  const res = await fetch(path, finalInit);
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`API ${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

export function getLibrary(query: LibraryQuery = {}): Promise<LibraryPage> {
  const params = new URLSearchParams();
  if (query.q) params.set("q", query.q);
  if (query.filter) params.set("filter", query.filter);
  if (query.sort_by) params.set("sort_by", query.sort_by);
  if (query.page !== undefined) params.set("page", String(query.page));
  if (query.page_size !== undefined)
    params.set("page_size", String(query.page_size));
  const qs = params.toString();
  return apiFetch<LibraryPage>(`/api/library${qs ? `?${qs}` : ""}`);
}

export function getAuthStatus(platform: string): Promise<AuthStatus> {
  return apiFetch<AuthStatus>(`/api/auth/${platform}`);
}

export function connectPlatform(platform: string): Promise<ConnectResult> {
  return apiFetch<ConnectResult>(`/api/auth/${platform}/connect`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
}

export function postSync(body: SyncRequest = {}): Promise<SyncDiff> {
  return apiFetch<SyncDiff>("/api/sync", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ offline: false, ...body }),
  });
}

export function applyPlatform(platform: string): Promise<ApplyResult> {
  return apiFetch<ApplyResult>(`/api/apply/${platform}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
}

/** Returns the raw URL for CSV export (used as an anchor href). */
export function exportCsvUrl(): string {
  return "/api/export.csv";
}

/** Opens an EventSource to /api/sync/stream and calls handlers as events arrive. */
export function openSyncStream(handlers: {
  onProgress: (stage: string) => void;
  onDone: (counts: Record<string, number>) => void;
  onError: (err: Event) => void;
}): EventSource {
  const es = new EventSource("/api/sync/stream");
  es.addEventListener("progress", (e: MessageEvent) => {
    handlers.onProgress((e as MessageEvent).data as string);
  });
  es.addEventListener("done", (e: MessageEvent) => {
    const counts = JSON.parse((e as MessageEvent).data as string) as Record<
      string,
      number
    >;
    handlers.onDone(counts);
    es.close();
  });
  es.onerror = (err) => {
    handlers.onError(err);
    es.close();
  };
  return es;
}
