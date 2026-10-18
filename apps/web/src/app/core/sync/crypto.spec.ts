import {
  decryptJson,
  deriveKeys,
  encryptJson,
  generateDataKey,
  unwrapDataKey,
  wrapDataKey,
} from './crypto';

describe('crypto', () => {
  const fast = { kdf: 'pbkdf2-sha256', iterations: 1000 };

  it('derives a deterministic auth key that is not the password, and a wrap key', async () => {
    const a = await deriveKeys('correct horse', 'Ann@Example.com', fast);
    const b = await deriveKeys('correct horse', 'ann@example.com ', fast);
    expect(a.authKey).toBe(b.authKey); // e-mail normalised
    expect(a.authKey).toHaveLength(64);
    expect(a.authKey).not.toContain('correct');
    const other = await deriveKeys('wrong horse', 'ann@example.com', fast);
    expect(other.authKey).not.toBe(a.authKey);
  });

  it('wraps the data key so another device can unwrap it with the password', async () => {
    const { wrapKey } = await deriveKeys('pw', 'a@b.c', fast);
    const dataKey = await generateDataKey();
    const wrapped = await wrapDataKey(dataKey, wrapKey);
    const unwrapped = await unwrapDataKey(wrapped, (await deriveKeys('pw', 'a@b.c', fast)).wrapKey);
    const blob = await encryptJson({ title: 'secret task' }, dataKey, 'account:1');
    expect(blob).not.toContain('secret');
    expect(await decryptJson(blob, unwrapped, 'account:1')).toEqual({ title: 'secret task' });
  });

  it('fails closed with a wrong key or a different context', async () => {
    const k1 = await generateDataKey();
    const k2 = await generateDataKey();
    const blob = await encryptJson({ x: 1 }, k1, 'ctx');
    await expect(decryptJson(blob, k2, 'ctx')).rejects.toThrow(/could not be decrypted/);
    await expect(decryptJson(blob, k1, 'other')).rejects.toThrow(/could not be decrypted/);
  });
});
