// Checks that every static translation key used in src/ exists in both locales,
// and that both locales expose the same key set.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const root = new URL("../src/", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : path.endsWith(".vue") || path.endsWith(".ts") ? [path] : [];
  });
}

function flatten(obj, prefix = "") {
  return Object.entries(obj).flatMap(([key, value]) =>
    typeof value === "object" && value !== null ? flatten(value, `${prefix}${key}.`) : [`${prefix}${key}`],
  );
}

const en = JSON.parse(readFileSync(join(root, "locales/en-US.json"), "utf8"));
const zh = JSON.parse(readFileSync(join(root, "locales/zh-CN.json"), "utf8"));
const enKeys = new Set(flatten(en));
const zhKeys = new Set(flatten(zh));

let failed = false;
for (const key of enKeys) if (!zhKeys.has(key)) (failed = true), console.log(`missing in zh-CN: ${key}`);
for (const key of zhKeys) if (!enKeys.has(key)) (failed = true), console.log(`missing in en-US: ${key}`);

const pattern = /\b\$?t\(\s*["']([^"'`]+)["']/g;
for (const file of walk(root)) {
  const source = readFileSync(file, "utf8");
  for (const match of source.matchAll(pattern)) {
    const key = match[1];
    if (!enKeys.has(key)) {
      failed = true;
      console.log(`${file.replace(root, "")}: unknown key ${key}`);
    }
  }
}
console.log(failed ? "i18n check FAILED" : `i18n check passed (${enKeys.size} keys)`);
process.exit(failed ? 1 : 0);
