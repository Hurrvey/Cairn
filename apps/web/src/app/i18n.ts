import { createI18n } from "vue-i18n";

import enUS from "@/locales/en-US.json";
import zhCN from "@/locales/zh-CN.json";

const SUPPORTED = ["en-US", "zh-CN"] as const;
export type Locale = (typeof SUPPORTED)[number];

function detect(): Locale {
  const stored = localStorage.getItem("cairn.locale");
  if (stored && (SUPPORTED as readonly string[]).includes(stored)) return stored as Locale;
  return navigator.language.startsWith("zh") ? "zh-CN" : "en-US";
}

const initialLocale = detect();
if (typeof document !== "undefined") document.documentElement.lang = initialLocale;

export const i18n = createI18n({
  legacy: false,
  locale: initialLocale,
  fallbackLocale: "en-US",
  messages: { "en-US": enUS, "zh-CN": zhCN },
  // Loud in development, silent in production: a missing key should fail review,
  // not spam a user's console.
  missingWarn: import.meta.env.DEV,
  fallbackWarn: import.meta.env.DEV,
});

export function setLocale(locale: Locale): void {
  i18n.global.locale.value = locale;
  localStorage.setItem("cairn.locale", locale);
  document.documentElement.lang = locale;
}
