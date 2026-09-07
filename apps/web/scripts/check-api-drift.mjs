/**
 * Fails if src/api/schema.d.ts is stale relative to openapi.json (T-M16-02).
 *
 * The failure this catches: a backend contract change ships, the frontend keeps
 * its old generated types, and the mismatch surfaces as a runtime error in a
 * user's browser rather than as a red build.
 *
 * Invokes the generator's JS entry point through `process.execPath` rather than
 * `npx`: no shell, so it behaves identically on Linux CI and Windows dev boxes.
 */
import { execFileSync } from "node:child_process";
import { readFileSync, rmSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const cli = join(dirname(require.resolve("openapi-typescript/package.json")), "bin", "cli.js");

const committed = join(root, "src", "api", "schema.d.ts");
const scratch = join(root, "src", "api", ".schema.check.d.ts");

try {
  execFileSync(process.execPath, [cli, join(root, "openapi.json"), "-o", scratch], {
    stdio: "inherit",
    cwd: root,
  });

  if (readFileSync(scratch, "utf8") !== readFileSync(committed, "utf8")) {
    console.error(
      "\nsrc/api/schema.d.ts is out of date with openapi.json.\n" +
        "Run `npm run api:generate` (or `make web-api` to re-export the spec first).\n",
    );
    process.exit(1);
  }
  console.log("API client is in sync with the spec.");
} finally {
  rmSync(scratch, { force: true });
}
