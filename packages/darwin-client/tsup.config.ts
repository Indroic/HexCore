import { defineConfig } from "tsup";

// Dual ESM + CJS, no ESM-only: Metro (Expo) y Jest siguen mordiendo con ESM-only, y este
// paquete existe justamente para que lo consuma una app de Expo (ver el README de la Fase 2
// del plan de monorepo). `entry` es un mapa y no un array a propósito: cada clave nombra el
// archivo de salida, que es lo que hace que agregar los subpaths `/webauthn` y `/store` (fases
// posteriores) no obligue a tocar `exports` en `package.json` más que para agregar la entrada.
export default defineConfig({
  entry: {
    index: "src/index.ts",
    webauthn: "src/webauthn.ts",
    store: "src/store.ts",
  },
  format: ["esm", "cjs"],
  dts: true,
  sourcemap: true,
  clean: true,
  splitting: false,
  // Cero dependencias de runtime (ver el README): no hay nada que tsup deba dejar externo
  // aparte de lo que Node/el bundler del consumidor resuelvan.
  treeshake: true,
});
