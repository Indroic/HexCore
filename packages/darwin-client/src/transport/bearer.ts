import type { TokenStorage } from "./storage";
import { memoryStorage } from "./storage";
import type { DarwinTokens, Transport } from "./types";

const CLAVE_ACCESS = "darwin.access_token";
const CLAVE_REFRESH = "darwin.refresh_token";

export interface BearerTransportOptions {
  /** Dónde guardar el par. Por defecto, `memoryStorage()` — ver su docstring. */
  storage?: TokenStorage;
}

/** El transporte Bearer: `Authorization: Bearer …`, tokens en el storage que se le pase. */
export class BearerTransport implements Transport {
  readonly name = "bearer" as const;

  private readonly storage: TokenStorage;

  constructor(options: BearerTransportOptions = {}) {
    this.storage = options.storage ?? memoryStorage();
  }

  async requestHeaders(_method: string): Promise<Record<string, string>> {
    const headers: Record<string, string> = { "X-Darwin-Transport": "bearer" };

    const token = await this.storage.get(CLAVE_ACCESS);
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }

    return headers;
  }

  async persist(tokens: DarwinTokens): Promise<void> {
    if (tokens.accessToken) {
      await this.storage.set(CLAVE_ACCESS, tokens.accessToken);
    }
    if (tokens.refreshToken) {
      await this.storage.set(CLAVE_REFRESH, tokens.refreshToken);
    }
  }

  async clear(): Promise<void> {
    await this.storage.remove(CLAVE_ACCESS);
    await this.storage.remove(CLAVE_REFRESH);
  }

  /** El refresh token guardado, para el fetcher (Fase 2 siguiente) — no forma parte de `Transport`. */
  async currentRefreshToken(): Promise<string | null> {
    return (await this.storage.get(CLAVE_REFRESH)) ?? null;
  }
}
