/**
 * Theme: light, dark, or follow the system. Stored in localStorage so a
 * reload does not flash the wrong scheme, and applied before the app mounts.
 */
import { computed, ref } from "vue";

export type ThemePreference = "light" | "dark" | "system";

const STORAGE_KEY = "cairn.theme";
const preference = ref<ThemePreference>("system");
const systemDark = ref(false);
let bound = false;

function readStored(): ThemePreference {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "light" || stored === "dark" || stored === "system") return stored;
  } catch {
    // Storage may be unavailable in private modes; fall through.
  }
  return "system";
}

function media(): MediaQueryList | null {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-color-scheme: dark)")
    : null;
}

function apply(): void {
  const dark = preference.value === "dark" || (preference.value === "system" && systemDark.value);
  document.documentElement.classList.toggle("dark", dark);
}

export function applyStoredTheme(): void {
  preference.value = readStored();
  const query = media();
  systemDark.value = query?.matches ?? false;
  if (query && !bound) {
    bound = true;
    query.addEventListener?.("change", (event) => {
      systemDark.value = event.matches;
      apply();
    });
  }
  apply();
}

export function useTheme() {
  const resolved = computed<"light" | "dark">(() =>
    preference.value === "dark" || (preference.value === "system" && systemDark.value)
      ? "dark"
      : "light",
  );

  function set(next: ThemePreference): void {
    preference.value = next;
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Ignore storage failures; the choice still applies for this session.
    }
    apply();
  }

  function toggle(): void {
    set(resolved.value === "dark" ? "light" : "dark");
  }

  return { preference, resolved, set, toggle };
}
