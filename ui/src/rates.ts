/**
 * Rates from running totals. The server and machines report totals since they started (bytes
 * through the network, requests answered); the console keeps the last sample and works out how
 * fast each one is growing.
 */
import { useEffect, useRef, useState } from "react";
import type { Sample } from "./components/LineChart";
import { formatBytes } from "./format";

/** Bytes a second. */
export const formatByteRate = (value: number) => `${formatBytes(Math.round(value))}/s`;

/** A chart's top: the largest value shown, rounded up so the line doesn't sit on the edge, and never below floor. */
export function ceiling(samples: Sample[], floor: number): number {
  const top = samples.reduce((most, sample) => Math.max(most, sample.v ?? 0), 0);
  return Math.max(floor, top * 1.2);
}

/** Days, hours and minutes in a number of seconds. */
export function span(seconds: number): string {
  const elapsed = Math.max(0, seconds);
  const days = Math.floor(elapsed / 86400);
  const hours = Math.floor((elapsed % 86400) / 3600);
  const minutes = Math.floor((elapsed % 3600) / 60);
  if (days) return `${days} d ${hours} h`;
  if (hours) return `${hours} h ${minutes} min`;
  return `${minutes} min`;
}

/**
 * The last windowSeconds of each total's rate a second, kept only while the page is open.
 * totals is null until the first sample; a total that's missing (an older program, a computer
 * that can't say) or that went backwards (a restart) leaves a gap rather than a spike.
 */
export function useRates<K extends string>(timestamp: number | undefined, totals: Record<K, number | null | undefined> | null, windowSeconds: number): Partial<Record<K, Sample[]>> {
  const history = useRef<Record<string, Sample[]>>({});
  const previous = useRef<{ t: number; totals: Record<string, number | null | undefined> } | null>(null);
  const [, setVersion] = useState(0);
  useEffect(() => {
    if (timestamp == null || !totals || previous.current?.t === timestamp) return;
    const before = previous.current;
    previous.current = { t: timestamp, totals };
    for (const key of Object.keys(totals)) {
      const series = (history.current[key] ??= []);
      const now = totals[key as K];
      const then = before?.totals[key];
      const elapsed = before ? timestamp - before.t : 0;
      const rate = now != null && then != null && elapsed > 0 && now >= then ? (now - then) / elapsed : null;
      // The first sample has nothing to compare with: it starts the clock and draws nothing.
      if (before) series.push({ t: timestamp, v: rate });
      while (series.length && series[0].t < timestamp - windowSeconds) series.shift();
    }
    setVersion((value) => value + 1);
  }, [timestamp, totals, windowSeconds]);
  return history.current as Partial<Record<K, Sample[]>>;
}
