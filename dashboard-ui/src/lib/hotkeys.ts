import { useEffect } from "react";

/**
 * Tiny hotkey hook. We deliberately avoid heavy keyboard libraries — this
 * component only needs a handful of bindings and they're declared at the
 * call site.
 *
 *   useHotkey("mod+k", () => openPalette())
 *   useHotkey(["g", "o"], () => navigate("/"))   // sequential keys
 */

type Combo = string | string[];

interface Options {
  enabled?: boolean;
  preventDefault?: boolean;
  /** Allow firing while focus is inside an editable element. */
  allowInInputs?: boolean;
}

const SEQ_TIMEOUT_MS = 1200;

function isEditable(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

function matches(combo: string, e: KeyboardEvent): boolean {
  const parts = combo.toLowerCase().split("+").map((p) => p.trim());
  const key = (e.key || "").toLowerCase();
  let need = { mod: false, shift: false, alt: false, ctrl: false };
  let target = "";
  for (const p of parts) {
    if (p === "mod") need.mod = true;
    else if (p === "ctrl") need.ctrl = true;
    else if (p === "shift") need.shift = true;
    else if (p === "alt" || p === "option") need.alt = true;
    else if (p === "cmd" || p === "meta") need.mod = true;
    else target = p;
  }
  if (target && key !== target) return false;
  const isMac =
    typeof navigator !== "undefined" && /mac/i.test(navigator.platform);
  const modOk = need.mod ? (isMac ? e.metaKey : e.ctrlKey) : true;
  return (
    modOk &&
    e.shiftKey === need.shift &&
    e.altKey === need.alt &&
    (need.ctrl ? e.ctrlKey : true)
  );
}

export function useHotkey(
  combo: Combo,
  handler: (e: KeyboardEvent) => void,
  options: Options = {},
): void {
  useEffect(() => {
    if (options.enabled === false) return;
    const isSeq = Array.isArray(combo);
    const seq: string[] = isSeq ? (combo as string[]) : [];
    let idx = 0;
    let lastAt = 0;

    function onKey(e: KeyboardEvent) {
      if (!options.allowInInputs && isEditable(e.target)) return;
      if (!isSeq) {
        if (matches(combo as string, e)) {
          if (options.preventDefault !== false) e.preventDefault();
          handler(e);
        }
        return;
      }
      // Sequential keys ("g" then "o").
      const now = Date.now();
      if (now - lastAt > SEQ_TIMEOUT_MS) idx = 0;
      lastAt = now;
      if (matches(seq[idx], e)) {
        idx += 1;
        if (idx === seq.length) {
          if (options.preventDefault !== false) e.preventDefault();
          handler(e);
          idx = 0;
        }
      } else {
        idx = 0;
      }
    }

    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [combo, handler, options.enabled, options.preventDefault, options.allowInInputs]);
}
