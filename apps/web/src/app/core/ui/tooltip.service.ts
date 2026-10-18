import { Injectable, inject, signal, untracked } from '@angular/core';
import { SettingsService } from '../settings/settings.service';
import { ToastService } from './toast.service';

/**
 * One tooltip for the whole app, driven by `data-tip` attributes (set by the `appTip`
 * directive). Desktop: hover / keyboard focus. Phones & tablets: press-and-hold, or Help mode
 * (the "?" button) where a tap explains a control instead of activating it. Uses the Popover
 * API so it renders above modal dialogs (older browsers fall back to a fixed element).
 */
@Injectable({ providedIn: 'root' })
export class TooltipService {
  private readonly settings = inject(SettingsService);
  private readonly toast = inject(ToastService);

  readonly text = signal('');
  readonly visible = signal(false);
  readonly helpMode = signal(false);
  readonly placement = signal<'above' | 'below'>('below');
  readonly position = signal({ top: 0, left: 0 });

  private host: HTMLElement | null = null;
  private target: HTMLElement | null = null;
  private hoverTimer: ReturnType<typeof setTimeout> | null = null;
  private pressTimer: ReturnType<typeof setTimeout> | null = null;
  private longPressed = false;
  private lastTouchAt = 0;

  attach(host: HTMLElement): void {
    this.host = host;
    const doc = document;

    doc.addEventListener('mouseover', (e) => {
      if (Date.now() - this.lastTouchAt < 1000) return;
      const t = tipTarget(e.target);
      if (!t || t === this.target) return;
      this.clearHover();
      this.hoverTimer = setTimeout(() => this.show(t), 350);
    });
    doc.addEventListener('mouseout', (e) => {
      const t = tipTarget(e.target);
      if (t && t === this.target && !t.contains(e.relatedTarget as Node)) this.hide();
      else if (t) this.clearHover();
    });
    doc.addEventListener('focusin', (e) => {
      const t = tipTarget(e.target);
      if (t) this.show(t);
    });
    doc.addEventListener('focusout', (e) => {
      if (tipTarget(e.target) === this.target) this.hide();
    });
    doc.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && this.visible()) {
        this.hide();
        if (this.helpMode()) this.setHelpMode(false);
      }
    });

    doc.addEventListener(
      'touchstart',
      (e) => {
        this.lastTouchAt = Date.now();
        this.longPressed = false;
        if (this.pressTimer) clearTimeout(this.pressTimer);
        const t = tipTarget(e.target);
        if (!t) {
          this.hide();
          return;
        }
        this.pressTimer = setTimeout(() => {
          this.longPressed = true;
          this.show(t);
        }, 450);
      },
      { passive: true },
    );
    const cancel = () => this.pressTimer && clearTimeout(this.pressTimer);
    doc.addEventListener('touchmove', cancel, { passive: true });
    doc.addEventListener('touchcancel', cancel, { passive: true });
    doc.addEventListener('touchend', (e) => {
      cancel();
      if (this.longPressed && e.cancelable) e.preventDefault(); // the hold asked "what is this?", not a tap
    });
    doc.addEventListener('contextmenu', (e) => {
      if (this.longPressed && tipTarget(e.target)) e.preventDefault();
    });

    doc.addEventListener(
      'click',
      (e) => {
        if (this.longPressed) {
          this.longPressed = false;
          e.preventDefault();
          e.stopPropagation();
          return;
        }
        if (!this.helpMode()) return;
        const t = tipTarget(e.target);
        if (!t || t.id === 'btn-help') return;
        e.preventDefault();
        e.stopPropagation();
        if (t === this.target && this.visible()) this.hide();
        else this.show(t);
      },
      true,
    );
    doc.addEventListener(
      'pointerdown',
      (e) => {
        if (this.helpMode() && tipTarget(e.target) && (e.target as HTMLElement).id !== 'btn-help')
          e.preventDefault();
      },
      true,
    );
    window.addEventListener(
      'scroll',
      () => {
        if (this.target && document.activeElement === this.target) this.reposition();
        else this.hide();
      },
      { passive: true, capture: true },
    );
    window.addEventListener('resize', () => this.hide());
  }

  show(target: HTMLElement): void {
    const text = target.getAttribute('data-tip');
    if (!text || !this.host) return;
    this.clearHover();
    this.target?.removeAttribute('aria-describedby');
    this.target = target;
    target.setAttribute('aria-describedby', 'tooltip');
    // DOM events can fire synchronously while Angular renders (e.g. focus moving when a dialog
    // opens); untracked() keeps these writes legal in that situation.
    untracked(() => {
      this.text.set(text);
      this.visible.set(true);
    });
    topLayerShow(this.host);
    // Position after the text has rendered.
    requestAnimationFrame(() => this.reposition());
  }

  /** Refresh text for the current target (tooltips with live content, e.g. the Update button). */
  refresh(target: HTMLElement): void {
    if (target === this.target && untracked(() => this.visible()))
      untracked(() => this.text.set(target.getAttribute('data-tip') ?? ''));
  }

  hide(): void {
    this.clearHover();
    this.target?.removeAttribute('aria-describedby');
    this.target = null;
    untracked(() => this.visible.set(false));
    if (this.host) topLayerHide(this.host);
  }

  setHelpMode(on: boolean): void {
    this.helpMode.set(on);
    document.body.classList.toggle('help-mode', on);
    this.hide();
    if (on)
      this.toast.show(
        'Help is on: tap any control to learn what it does. Tap ? again to turn it off.',
      );
  }

  maybeShowFirstRunHint(): void {
    const coarse = window.matchMedia?.('(pointer: coarse)').matches;
    if (coarse && !this.settings.settings().helpHintShown) {
      this.settings.update({ helpHintShown: true });
      setTimeout(
        () =>
          this.toast.show(
            'Tip: press and hold any button to see what it does, or tap ? for help mode.',
          ),
        1500,
      );
    }
  }

  private reposition(): void {
    if (!this.target || !this.host) return;
    const r = this.target.getBoundingClientRect();
    const margin = 8;
    const tw = this.host.offsetWidth,
      th = this.host.offsetHeight;
    let top = r.bottom + margin;
    let placement: 'above' | 'below' = 'below';
    if (top + th > window.innerHeight - margin && r.top - th - margin > 0) {
      top = r.top - th - margin;
      placement = 'above';
    }
    const left = Math.max(
      margin,
      Math.min(r.left + r.width / 2 - tw / 2, window.innerWidth - tw - margin),
    );
    untracked(() => {
      this.position.set({ top, left });
      this.placement.set(placement);
    });
  }

  private clearHover() {
    if (this.hoverTimer) clearTimeout(this.hoverTimer);
    this.hoverTimer = null;
  }
}

function tipTarget(node: EventTarget | null): HTMLElement | null {
  return node instanceof Element ? (node.closest('[data-tip]') as HTMLElement | null) : null;
}

export function topLayerShow(el: HTMLElement) {
  const p = el as HTMLElement & { showPopover?: () => void };
  if (p.showPopover && !el.matches(':popover-open')) {
    try {
      p.showPopover();
    } catch {
      /* unsupported */
    }
  }
}

export function topLayerHide(el: HTMLElement) {
  const p = el as HTMLElement & { hidePopover?: () => void };
  if (p.hidePopover && el.matches(':popover-open')) {
    try {
      p.hidePopover();
    } catch {
      /* unsupported */
    }
  }
}
