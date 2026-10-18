import { Injectable, signal } from '@angular/core';

export interface Toast {
  message: string;
  error: boolean;
  id: number;
}

@Injectable({ providedIn: 'root' })
export class ToastService {
  readonly current = signal<Toast | null>(null);
  private seq = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;

  show(message: string, error = false): void {
    if (this.timer) clearTimeout(this.timer);
    this.current.set({ message, error, id: ++this.seq });
    this.timer = setTimeout(() => this.current.set(null), error ? 6000 : 2500);
  }

  error(message: string) {
    this.show(message, true);
  }
}
