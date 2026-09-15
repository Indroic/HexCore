/** Junta una base y un path sin duplicar ni perder la barra del medio. */
export function joinUrl(base: string, path: string): string {
  const baseSinBarra = base.replace(/\/+$/, "");
  const pathConBarra = path.startsWith("/") ? path : `/${path}`;
  return `${baseSinBarra}${pathConBarra}`;
}
