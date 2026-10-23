import {
  Component,
  DestroyRef,
  ElementRef,
  effect,
  inject,
  input,
  signal,
  viewChild,
} from '@angular/core';
import { TipDirective } from '../ui/tip.directive';
import { type AdEmbed as AdEmbedSpec, type AdSource, AdsService } from './ads.service';

type Status = 'idle' | 'loading' | 'loaded' | 'rendered' | 'failed';

/** One script per URL per page, however often the panel re-renders. */
const loadedScripts = new Map<string, Promise<void>>();

function loadScript(url: string, attrs: Record<string, string>): Promise<void> {
  let p = loadedScripts.get(url);
  if (!p) {
    p = new Promise<void>((resolve, reject) => {
      const s = document.createElement('script');
      s.async = true;
      for (const [k, v] of Object.entries(attrs)) s.setAttribute(k, v);
      s.onload = () => resolve();
      s.onerror = () => reject(new Error(`failed to load ${url}`));
      s.src = url;
      document.head.appendChild(s);
    });
    loadedScripts.set(url, p);
    p.catch(() => loadedScripts.delete(url));
  }
  return p;
}

declare global {
  interface Window {
    __paFakeAdRendered?: (kind: string, el: Element) => void;
  }
}

/**
 * Third-party ad slot (Google AdSense, Microsoft Advertising, a network's tag).
 *
 * Renders exactly the markup the provider documents — a container element with the provider's
 * data attributes plus the provider's script — but only after the user has allowed that
 * provider (the consent card). Fake providers (`mode: 'fake'`) go through the very same path
 * with a script served by our own API, so the integration is tested end to end without a
 * provider account and without any request leaving the machine.
 */
@Component({
  selector: 'app-ad-embed',
  imports: [TipDirective],
  template: `
    @if (!ads.embedAllowed()) {
      <div class="ad-consent" id="ad-consent">
        @if (ads.consentState() === 'denied') {
          <p>
            You declined ads from <strong>{{ source().provider_name }}</strong
            >. Nothing from them has been loaded on this device.
          </p>
        } @else {
          <p>
            Ads here are provided by <strong>{{ source().provider_name }}</strong
            >, a third party. Loading them lets {{ source().provider_name }} set cookies and process
            your IP address and device information under its own
            @if (source().privacy_url) {
              <a [href]="source().privacy_url" target="_blank" rel="noopener noreferrer"
                >privacy policy</a
              >.
            } @else {
              privacy policy.
            }
            Nothing is loaded until you allow it.
          </p>
        }
        <div class="ad-consent-actions">
          <button
            type="button"
            class="btn primary"
            id="ad-consent-allow"
            (click)="ads.setConsent(source().kind, 'granted')"
            appTip="Load the provider's ad script on this device. You can withdraw this under “Why this?” at any time."
          >
            Allow {{ source().provider_name }} ads
          </button>
          @if (ads.consentState() !== 'denied') {
            <button
              type="button"
              class="btn"
              id="ad-consent-deny"
              (click)="ads.setConsent(source().kind, 'denied')"
              appTip="Do not load anything from this provider. The panel stays empty."
            >
              Not now
            </button>
          }
        </div>
      </div>
    } @else {
      <div class="ad-slot" #slot [attr.data-status]="status()"></div>
      <p class="ad-slot-status muted small" id="ad-slot-status">
        @switch (status()) {
          @case ('loading') {
            Loading {{ source().provider_name }} tag…
          }
          @case ('loaded') {
            {{ source().provider_name }} tag loaded — waiting for a creative.
          }
          @case ('rendered') {
            Creative rendered by {{ source().provider_name
            }}{{ source().mode === 'fake' ? ' (fake tag served by this API)' : '' }}.
          }
          @case ('failed') {
            The {{ source().provider_name }} tag could not be loaded (blocked or offline).
          }
        }
      </p>
    }
  `,
})
export class AdEmbedComponent {
  readonly ads = inject(AdsService);
  readonly source = input.required<AdSource>();
  readonly status = signal<Status>('idle');

  private readonly slot = viewChild<ElementRef<HTMLElement>>('slot');
  private mountedFor: string | null = null;

  constructor() {
    // Fake provider scripts call this when they have drawn into our container.
    if (typeof window !== 'undefined') {
      const prev = window.__paFakeAdRendered;
      window.__paFakeAdRendered = (kind, el) => {
        prev?.(kind, el);
        if (this.slot()?.nativeElement.contains(el)) this.status.set('rendered');
      };
    }
    effect(() => {
      const host = this.slot()?.nativeElement;
      const src = this.source();
      const allowed = this.ads.embedAllowed();
      if (!host || !allowed || !src.embed) return;
      const key = `${src.kind}|${src.embed.script_url}`;
      if (this.mountedFor === key) return;
      this.mountedFor = key;
      void this.mount(host, src.embed);
    });
    inject(DestroyRef).onDestroy(() => (this.mountedFor = null));
  }

  private async mount(host: HTMLElement, spec: AdEmbedSpec) {
    host.replaceChildren();
    const el = document.createElement(spec.container_tag);
    el.className = spec.container_class;
    if (spec.container_style) el.setAttribute('style', spec.container_style);
    for (const [k, v] of Object.entries(spec.container_attrs)) el.setAttribute(k, v);
    host.appendChild(el);
    this.status.set('loading');
    try {
      await loadScript(spec.script_url, spec.script_attrs ?? {});
      if (spec.push_global) {
        const w = window as unknown as Record<string, { push: (x: object) => unknown }>;
        (w[spec.push_global] =
          w[spec.push_global] || ([] as unknown as { push: (x: object) => unknown })).push({});
      }
      if (this.status() !== 'rendered') this.status.set('loaded');
      // Real tags draw an iframe into the container; report that too.
      const check = () => {
        if (el.querySelector('iframe, .fake-ad-box')) this.status.set('rendered');
      };
      setTimeout(check, 1500);
      setTimeout(check, 5000);
    } catch {
      this.status.set('failed');
    }
  }
}
