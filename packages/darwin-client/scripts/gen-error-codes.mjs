#!/usr/bin/env node
// Genera src/generated/error-codes.ts desde openapi/darwin.errors.json.
//
// El código es el discriminante de `DarwinError` (ver el README de la Fase 2 del plan): con
// esto generado, `switch (err.code)` es exhaustivo y el compilador avisa si el backend agrega
// un código nuevo — la alternativa (18 subclases a mano) es código muerto que hay que
// sincronizar manualmente con `IDENTITY_EXCEPTION_STATUS_MAP` y los seis plugins.
//
// Uso: node scripts/gen-error-codes.mjs [--check]

import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const errorsJsonPath = path.join(root, "openapi", "darwin.errors.json");
const outPath = path.join(root, "src", "generated", "error-codes.ts");

function generar() {
  const errores = JSON.parse(readFileSync(errorsJsonPath, "utf-8"));
  const nombres = Object.keys(errores).sort();

  const entradas = nombres.map((nombre) => `  ${nombre}: ${errores[nombre]},`).join("\n");

  return `// Generado por scripts/gen-error-codes.mjs desde openapi/darwin.errors.json.
// No editar a mano: se sobreescribe con \`npm run gen:errors\`.

/** Los códigos de error de Darwin y su status HTTP, tal como los emite el servidor. */
export const ERROR_CODES = {
${entradas}
} as const;

/** El discriminante de \`DarwinError.code\`: un \`switch\` sobre esto es exhaustivo. */
export type DarwinErrorCode = keyof typeof ERROR_CODES;
`;
}

const contenido = generar();
const check = process.argv.includes("--check");

if (check) {
  const actual = existsSync(outPath) ? readFileSync(outPath, "utf-8") : null;
  if (actual !== contenido) {
    console.error(
      "::error::src/generated/error-codes.ts está desactualizado respecto a " +
        "openapi/darwin.errors.json. Regenerá con `npm run gen:errors`.",
    );
    process.exit(1);
  }
  console.log("error-codes.ts: en verde (sin drift).");
  process.exit(0);
}

writeFileSync(outPath, contenido, "utf-8");
console.log(`escrito ${outPath}`);
