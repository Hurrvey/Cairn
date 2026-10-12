import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import openapiTS, { astToString } from "openapi-typescript";

const here = dirname(fileURLToPath(import.meta.url));
const schemaPath = join(here, "..", "..", "apps", "web", "openapi.json");
const outputPath = join(here, "src", "schema.ts");
const schema = await readFile(schemaPath, "utf8");
const ast = await openapiTS(JSON.parse(schema), { defaultNonNullable: false });
const digest = createHash("sha256").update(schema).digest("hex");
const generated = `// OpenAPI SHA256: ${digest}\n${astToString(ast)}`;
if (process.argv.includes("--check")) {
  const current = await readFile(outputPath, "utf8");
  if (current !== generated) {
    throw new Error("Generated schema types are stale; run npm run generate");
  }
} else {
  await writeFile(outputPath, generated, "utf8");
}
