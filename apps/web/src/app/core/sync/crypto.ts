/**
 * Client-side cryptography (WebCrypto only — works in every modern browser and in Node ≥ 20).
 *
 *   password + email ──PBKDF2-SHA256──▶ master key (32 B, never leaves the device)
 *   master ──HKDF("auth")──▶ auth key   → sent to the server as the login verifier
 *   master ──HKDF("wrap")──▶ wrap key   → wraps a random data key; the wrapped key is stored on
 *                                        the server so another device can unwrap it after login
 *   data key ──AES-256-GCM──▶ every sync op and snapshot (the server relays ciphertext only)
 *
 * Parameters are pinned in KDF_PARAMS and sent with the account so they can be raised later
 * (or swapped for Argon2id via WASM) without breaking existing accounts.
 */

export const KDF_PARAMS = {
  kdf: 'pbkdf2-sha256',
  iterations: 600_000,
  hkdf: 'sha256',
  v: 1,
} as const;
export type KdfParams = { kdf: string; iterations: number; hkdf?: string; v?: number };

const enc = new TextEncoder();
const dec = new TextDecoder();

export function toBase64(bytes: ArrayBuffer | Uint8Array): string {
  const u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let s = '';
  for (let i = 0; i < u8.length; i += 0x8000)
    s += String.fromCharCode(...u8.subarray(i, i + 0x8000));
  return btoa(s);
}

export function fromBase64(text: string): Uint8Array<ArrayBuffer> {
  const s = atob(text);
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

export function toHex(bytes: ArrayBuffer | Uint8Array): string {
  return [...(bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes))]
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

export interface DerivedKeys {
  authKey: string; // hex, sent to the server
  wrapKey: CryptoKey; // AES-GCM, stays on the device
}

export async function deriveKeys(
  password: string,
  email: string,
  params: KdfParams = KDF_PARAMS,
): Promise<DerivedKeys> {
  const salt = await crypto.subtle.digest('SHA-256', enc.encode(email.trim().toLowerCase()));
  const base = await crypto.subtle.importKey(
    'raw',
    enc.encode(password.normalize('NFKC')),
    'PBKDF2',
    false,
    ['deriveBits'],
  );
  const master = await crypto.subtle.deriveBits(
    { name: 'PBKDF2', hash: 'SHA-256', salt, iterations: params.iterations },
    base,
    256,
  );
  const hkdfKey = await crypto.subtle.importKey('raw', master, 'HKDF', false, [
    'deriveBits',
    'deriveKey',
  ]);
  const authBits = await crypto.subtle.deriveBits(
    { name: 'HKDF', hash: 'SHA-256', salt: new Uint8Array(32), info: enc.encode('auth') },
    hkdfKey,
    256,
  );
  const wrapKey = await crypto.subtle.deriveKey(
    { name: 'HKDF', hash: 'SHA-256', salt: new Uint8Array(32), info: enc.encode('wrap') },
    hkdfKey,
    { name: 'AES-GCM', length: 256 },
    false,
    ['encrypt', 'decrypt'],
  );
  return { authKey: toHex(authBits), wrapKey };
}

export async function generateDataKey(): Promise<CryptoKey> {
  return crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, true, ['encrypt', 'decrypt']);
}

export async function wrapDataKey(dataKey: CryptoKey, wrapKey: CryptoKey): Promise<string> {
  const raw = await crypto.subtle.exportKey('raw', dataKey);
  return encryptBytes(new Uint8Array(raw), wrapKey, 'data-key');
}

export async function unwrapDataKey(wrapped: string, wrapKey: CryptoKey): Promise<CryptoKey> {
  const raw = await decryptBytes(wrapped, wrapKey, 'data-key');
  // Non-extractable from here on: the key can be used but never read back out of IndexedDB.
  return crypto.subtle.importKey('raw', raw, { name: 'AES-GCM' }, false, ['encrypt', 'decrypt']);
}

/** AES-256-GCM; output = base64(iv ‖ ciphertext ‖ tag). `aad` binds the blob to its purpose. */
export async function encryptBytes(
  plain: Uint8Array<ArrayBuffer>,
  key: CryptoKey,
  aad: string,
): Promise<string> {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv, additionalData: enc.encode(aad) },
    key,
    plain,
  );
  const out = new Uint8Array(iv.length + ct.byteLength);
  out.set(iv, 0);
  out.set(new Uint8Array(ct), iv.length);
  return toBase64(out);
}

export async function decryptBytes(
  blob: string,
  key: CryptoKey,
  aad: string,
): Promise<Uint8Array<ArrayBuffer>> {
  const bytes = fromBase64(blob);
  const iv = bytes.subarray(0, 12);
  const ct = bytes.subarray(12);
  try {
    return new Uint8Array(
      await crypto.subtle.decrypt(
        { name: 'AES-GCM', iv, additionalData: enc.encode(aad) },
        key,
        ct,
      ),
    );
  } catch {
    throw new Error('The data could not be decrypted with this account\u2019s key.');
  }
}

export async function encryptJson(value: unknown, key: CryptoKey, aad: string): Promise<string> {
  return encryptBytes(enc.encode(JSON.stringify(value)), key, aad);
}

export async function decryptJson<T>(blob: string, key: CryptoKey, aad: string): Promise<T> {
  return JSON.parse(dec.decode(await decryptBytes(blob, key, aad))) as T;
}
