import { compareVersions, isNewer } from './version';

describe('compareVersions', () => {
  it('orders semantic versions', () => {
    expect(compareVersions('1.2.3', '1.2.3')).toBe(0);
    expect(compareVersions('1.10.0', '1.9.9')).toBe(1);
    expect(compareVersions('1.0.0-beta.1', '1.0.0')).toBe(-1);
    expect(compareVersions('1.0.0-beta.2', '1.0.0-beta.1')).toBe(1);
    expect(compareVersions('1.0.0-rc.1', '1.0.0-beta.9')).toBe(1);
    expect(compareVersions('v2.0.0', '2.0.0')).toBe(0);
    expect(isNewer('1.0.1', '1.0.0')).toBe(true);
    expect(isNewer('garbage', '1.0.0')).toBe(false);
  });
});
