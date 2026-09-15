/**
 * Dónde vive el par de tokens en el transporte Bearer.
 *
 * Cada método puede ser sync o async: `memoryStorage` es sync, pero `fromAsyncStorage`
 * envuelve algo como `expo-secure-store`, que es async por naturaleza.
 */
export interface TokenStorage {
  get(key: string): string | null | Promise<string | null>;
  set(key: string, value: string): void | Promise<void>;
  remove(key: string): void | Promise<void>;
}

/**
 * El storage por defecto de `BearerTransport`, y el único que debería serlo.
 *
 * `localStorage` nunca es el default: es legible por cualquier JS del origen, así que un XSS
 * se lleva el par entero, y el refresh vale semanas con rotación — exfiltrarlo es una sesión
 * persistente, no dos minutos. Es exactamente lo que `CookieTransport` evita con `HttpOnly`.
 * Vive sólo en memoria del proceso: se pierde al recargar la página, lo cual es correcto para
 * el default — quien necesite persistencia entre recargas elige `localStorageAdapter()` (o
 * `fromAsyncStorage()` en nativo) sabiendo el trade-off.
 */
export function memoryStorage(): TokenStorage {
  const valores = new Map<string, string>();
  return {
    get(key) {
      return valores.get(key) ?? null;
    },
    set(key, value) {
      valores.set(key, value);
    },
    remove(key) {
      valores.delete(key);
    },
  };
}

/**
 * `localStorage` del navegador. Se exporta igual que `memoryStorage` porque hay casos
 * legítimos (una extensión, un dashboard interno de solo-lectura) y ocultarlo empuja a
 * escribirlo peor a mano. No es el default — ver el docstring de `memoryStorage`.
 */
export function localStorageAdapter(): TokenStorage {
  return {
    get(key) {
      try {
        return localStorage.getItem(key);
      } catch {
        return null;
      }
    },
    set(key, value) {
      try {
        localStorage.setItem(key, value);
      } catch {
        // Storage lleno, modo privado que lo bloquea, etc. El storage es best-effort: fallar
        // acá no puede tirar abajo un sign-in que ya sucedió del lado del servidor.
      }
    },
    remove(key) {
      try {
        localStorage.removeItem(key);
      } catch {
        // Ídem.
      }
    },
  };
}

/** Lo que un storage async de React Native (SecureStore, AsyncStorage) expone. */
export interface AsyncStorageLike {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
  removeItem(key: string): Promise<void>;
}

/** Envuelve un storage async nativo (`expo-secure-store`, Keychain) como `TokenStorage`. */
export function fromAsyncStorage(storage: AsyncStorageLike): TokenStorage {
  return {
    get: (key) => storage.getItem(key),
    set: (key, value) => storage.setItem(key, value),
    remove: (key) => storage.removeItem(key),
  };
}
