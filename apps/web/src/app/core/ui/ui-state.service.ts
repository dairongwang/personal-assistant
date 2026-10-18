import { Injectable, computed, signal } from '@angular/core';

/** Whether it is a good moment to reload the app (no dialog open, user not typing, not busy). */
@Injectable({ providedIn: 'root' })
export class UiStateService {
  readonly dialogsOpen = signal(0);
  readonly busy = signal(0);
  private readonly tick = signal(0);

  readonly safeToReload = computed(() => {
    this.tick();
    return this.dialogsOpen() === 0 && this.busy() === 0 && !isTyping();
  });

  /** Re-evaluate (e.g. after focus changes) — called by the shell on focus/visibility changes. */
  poke(): void {
    this.tick.update((n) => n + 1);
  }

  dialogOpened() {
    this.dialogsOpen.update((n) => n + 1);
  }
  dialogClosed() {
    this.dialogsOpen.update((n) => Math.max(0, n - 1));
  }
}

export function isTyping(): boolean {
  const a = document.activeElement as HTMLElement | null;
  if (!a) return false;
  const tag = a.tagName;
  return (
    (tag === 'INPUT' && (a as HTMLInputElement).type !== 'checkbox') ||
    tag === 'TEXTAREA' ||
    tag === 'SELECT' ||
    a.isContentEditable
  );
}
