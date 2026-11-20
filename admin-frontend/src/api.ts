export class SessionExpired extends Error {}

export async function api<T>(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`/admin/api${path}`, {
    credentials: "same-origin",
    cache: "no-store",
    signal,
    ...(body === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
  });
  signal?.throwIfAborted();
  if (response.status === 401 && path !== "/login") {
    if (path !== "/session")
      window.dispatchEvent(new Event("admin-session-expired"));
    throw new SessionExpired("Session ended. Sign in again.");
  }
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const error = (await response.json()) as { detail?: unknown };
      if (typeof error.detail === "string") message = error.detail;
    } catch {
      /* Responses from proxies may not be JSON. */
    }
    throw new Error(message);
  }
  const contentType = response.headers.get("content-type")?.split(";", 1)[0];
  if (contentType?.trim().toLowerCase() !== "application/json") {
    throw new Error(
      "The Admin API returned an unexpected response. Make sure you are using the Admin service URL.",
    );
  }
  try {
    return (await response.json()) as T;
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    throw new Error("The Admin API returned invalid JSON.", { cause: error });
  }
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Request failed.";
}

export type Row = Record<string, unknown>;
export type Snapshot = {
  overview: {
    worker_available: boolean;
    database_backend: string;
    database_bytes: number;
    format_version: number;
    references: { published: number; stored: number };
    submissions: Record<string, number>;
    manual_review: number | null;
  };
  revisions: Record<string, string>;
  updated_at: string;
  failed: boolean;
};

export function display(value: unknown, field = ""): string {
  if (value === null || value === undefined) return "—";
  if (field === "created_at" && typeof value === "number") {
    return new Date(value * 1000)
      .toISOString()
      .replace("T", " ")
      .replace(".000Z", " UTC");
  }
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}
