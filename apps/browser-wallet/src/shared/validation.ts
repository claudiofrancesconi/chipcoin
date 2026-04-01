import { MIN_PASSWORD_LENGTH } from "./constants";

export function requireMinPasswordLength(password: string): void {
  if (password.length < MIN_PASSWORD_LENGTH) {
    throw new Error(`Password must be at least ${MIN_PASSWORD_LENGTH} characters long.`);
  }
}

export function normalizeNodeEndpoint(value: string): string {
  const trimmed = value.trim().replace(/\/+$/, "");
  if (!trimmed) {
    throw new Error("Node API endpoint is required.");
  }
  let url: URL;
  try {
    url = new URL(trimmed);
  } catch (error) {
    throw new Error("Node API endpoint must be a valid URL.");
  }
  if (!["http:", "https:"].includes(url.protocol)) {
    throw new Error("Node API endpoint must use http or https.");
  }
  return url.toString().replace(/\/+$/, "");
}
