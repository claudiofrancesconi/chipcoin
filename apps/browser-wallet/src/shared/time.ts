export function minutesToMilliseconds(minutes: number): number {
  return minutes * 60 * 1000;
}

export function unixToIso(value: number | null | undefined): string {
  if (value == null) {
    return "Unknown";
  }
  return new Date(value * 1000).toISOString();
}
