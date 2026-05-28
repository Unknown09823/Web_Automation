import { create } from "zustand";

interface UIState {
  /** Off-canvas mobile sidebar */
  sidebarOpen: boolean;
  setSidebarOpen: (v: boolean) => void;

  /** Command palette */
  paletteOpen: boolean;
  setPaletteOpen: (v: boolean) => void;
  togglePalette: () => void;

  /** Polling cadence in ms — operator-tunable from the topbar */
  pollIntervalMs: number;
  setPollIntervalMs: (ms: number) => void;

  /** Pause polling globally (e.g., when Live Browser is in foreground) */
  pollingPaused: boolean;
  setPollingPaused: (v: boolean) => void;
}

export const useUIStore = create<UIState>((set) => ({
  sidebarOpen: false,
  setSidebarOpen: (v) => set({ sidebarOpen: v }),

  paletteOpen: false,
  setPaletteOpen: (v) => set({ paletteOpen: v }),
  togglePalette: () => set((s) => ({ paletteOpen: !s.paletteOpen })),

  pollIntervalMs: 4000,
  setPollIntervalMs: (ms) => set({ pollIntervalMs: ms }),

  pollingPaused: false,
  setPollingPaused: (v) => set({ pollingPaused: v }),
}));
