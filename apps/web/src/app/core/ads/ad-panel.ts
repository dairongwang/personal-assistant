import {
  Component,
  DestroyRef,
  ElementRef,
  computed,
  effect,
  inject,
  output,
  viewChild,
} from '@angular/core';
import { TipDirective } from '../ui/tip.directive';
import { AdEmbedComponent } from './ad-embed';
import { type AdCreative, AdsService } from './ads.service';
import { NO_REGION, countryName, languageName } from './region';

const VIEWABLE_RATIO = 0.5; // IAB: at least half of the creative …
const VIEWABLE_MS = 1000; // … in view for at least one second = one impression

/**
 * The sponsored-content panel: one small, clearly labelled card at the end of the sidebar.
 *
 * Non-intrusive by construction: it is part of the normal page flow (never fixed, never an
 * overlay, never a modal), has no animation or sound, shows one text-first creative at a time,
 * can be folded to a one-line strip or hidden per creative, and its link opens in a new tab
 * without a referrer. It renders nothing at all for memberships with `ad_free`.
 */
@Component({
  selector: 'app-ad-panel',
  imports: [TipDirective, AdEmbedComponent],
  template: `
    @if (ads.eligible() && (ads.current() || ads.embedMode() || ads.collapsed() || ads.large())) {
      <aside
        class="card ad-panel"
        id="ad-panel"
        aria-labelledby="ad-label"
        [class.collapsed]="ads.collapsed()"
        [class.large]="ads.large()"
        [attr.data-ad-id]="ads.current()?.id ?? null"
        [attr.data-source]="ads.source()?.kind ?? null"
        [attr.data-region]="ads.region() ?? ''"
        [attr.data-lang]="ads.language() ?? ''"
      >
        <div class="ad-head">
          <span class="ad-badge" id="ad-label">{{ ads.badge() }}</span>
          @if (ads.source()?.mode === 'fake') {
            <span
              class="ad-fake"
              id="ad-fake"
              title="Built-in fake provider — nothing leaves this machine"
              >FAKE</span
            >
          }
          @if (ads.current(); as ad) {
            <span class="ad-sponsor">{{ ad.sponsor }}</span>
          } @else if (ads.embedMode()) {
            <span class="ad-sponsor muted">{{ ads.source()?.provider_name }}</span>
          } @else if (ads.collapsed()) {
            <span class="ad-sponsor muted">hidden</span>
          } @else {
            <span class="ad-sponsor muted">{{ ads.loading() ? 'loading…' : 'no message' }}</span>
          }
          <span class="ad-tools">
            <button
              type="button"
              class="ad-tool"
              id="ad-why"
              (click)="why.emit()"
              appTip="Why am I seeing this? Shows what is used to pick sponsored content (only your country and language, chosen on this device), what is never used, and where to change or turn it off."
            >
              Why this?
            </button>
            <button
              type="button"
              class="ad-tool"
              id="ad-toggle"
              (click)="ads.setCollapsed(!ads.collapsed())"
              [attr.aria-expanded]="!ads.collapsed()"
              aria-controls="ad-body"
              [appTip]="
                ads.collapsed()
                  ? 'Show the sponsored message again.'
                  : 'Fold the sponsored message away to a one-line strip. Nothing is requested while it is folded.'
              "
            >
              {{ ads.collapsed() ? 'Show' : 'Hide' }}
            </button>
          </span>
        </div>
        @if (!ads.collapsed()) {
          <!-- Targeting, spelled out on the panel: the country + language creatives are picked for. -->
          <p class="ad-target" id="ad-target">
            <span class="ad-target-label">Shown for</span>
            <mark class="ad-hl" id="ad-target-country">{{ countryLabel() }}</mark>
            <span class="ad-plus">+</span>
            <mark class="ad-hl" id="ad-target-lang">{{ languageLabel() }}</mark>
            <span class="ad-target-source">{{ sourceLabel() }}</span>
          </p>
        }
        @if (ads.embedMode() && !ads.collapsed() && ads.source(); as src) {
          <div class="ad-body ad-body-embed" id="ad-embed">
            <app-ad-embed [source]="src" />
          </div>
          @if (ads.large()) {
            <p class="ad-debug muted small" id="ad-debug">
              Source: {{ src.title }} ({{ src.mode }}) · rendered by the provider's script ·
              reporting in the provider's console
            </p>
          }
        } @else if (ads.current(); as ad) {
          <div class="ad-body" id="ad-body" #creative>
            <a
              class="ad-link"
              [href]="safeHref(ad)"
              [attr.target]="isAppLink(ad) ? null : '_blank'"
              rel="noopener noreferrer nofollow sponsored"
              referrerpolicy="no-referrer"
              (click)="onLinkClick($event, ad)"
              [attr.aria-label]="
                'Sponsored: ' + ad.headline + ' — ' + ad.sponsor + '. Opens in a new tab.'
              "
              appTip="Sponsored link. Opens the sponsor's page in a new tab; the sponsor is not told where you came from."
            >
              @if (ad.image_url) {
                <img
                  class="ad-img"
                  [src]="ad.image_url"
                  alt=""
                  width="48"
                  height="48"
                  loading="lazy"
                  decoding="async"
                  referrerpolicy="no-referrer"
                />
              }
              <span class="ad-text">
                <span class="ad-headline">{{ ad.headline }}</span>
                @if (ad.blurb) {
                  <span class="ad-blurb">{{ ad.blurb }}</span>
                }
                <span class="ad-cta">{{ ad.cta_label }} {{ isAppLink(ad) ? '→' : '↗' }}</span>
                @if (ads.source()?.disclosure; as disclosure) {
                  <span
                    class="ad-disclosure"
                    id="ad-disclosure"
                    [class.prominent]="ads.source()?.kind === 'affiliate'"
                    >{{ disclosure }}</span
                  >
                }
              </span>
            </a>
            <button
              type="button"
              class="ad-dismiss"
              id="ad-dismiss"
              (click)="ads.dismiss(ad)"
              aria-label="Hide this ad for a week"
              appTip="Hide this particular message on this device for a week."
            >
              ✕
            </button>
          </div>
          @if (ads.large()) {
            <p class="ad-debug muted small" id="ad-debug">
              Source: {{ ads.source()?.title }} ({{ ads.source()?.mode }}) · campaign
              {{ ads.visible().indexOf(ad) + 1 }} of {{ ads.visible().length }} for this context ·
              this session: {{ ads.stats().impressions }} view{{
                ads.stats().impressions === 1 ? '' : 's'
              }}
              · {{ ads.stats().clicks }} click{{ ads.stats().clicks === 1 ? '' : 's' }} counted
              (anonymous)
            </p>
          }
        } @else if (ads.large() && !ads.collapsed()) {
          <!-- Showcase layout only: say why the panel is empty instead of vanishing. -->
          <p class="ad-empty" id="ad-empty">
            @if (!ads.online()) {
              Offline — sponsored content is only requested while online.
            } @else if (ads.loading()) {
              Loading sponsored content…
            } @else if (ads.lastError()) {
              The sponsored-content service did not answer ({{ ads.lastError() }}).
            } @else if (ads.source()?.mode === 'unavailable') {
              The configured ad source ({{ ads.source()?.title }}) is not set up on the server.
            } @else {
              No {{ ads.source()?.title?.toLowerCase() ?? 'campaign' }} for {{ countryLabel() }} +
              {{ languageLabel() }} right now.
              <span class="muted"
                >Developers: <code>python dev.py seed --ads</code> creates dummy campaigns.</span
              >
            }
          </p>
        }
      </aside>
    }
  `,
})
export class AdPanel {
  readonly ads = inject(AdsService);
  /** "Why this?" — the shell opens the explanation dialog. */
  readonly why = output<void>();
  /** An internal (house) ad linking into the app — the shell opens the account screen. */
  readonly upgrade = output<void>();

  private readonly creative = viewChild<ElementRef<HTMLElement>>('creative');
  private observer: IntersectionObserver | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private watching: AdCreative | null = null;

  constructor() {
    // Viewability: start a 1 s timer when ≥50 % of the creative is on screen; count the
    // impression if it is still there when the timer fires. Re-armed for every new creative.
    effect(() => {
      const ad = this.ads.current();
      const el = this.creative()?.nativeElement ?? null;
      this.unwatch();
      if (ad && el) this.watch(ad, el);
    });
    inject(DestroyRef).onDestroy(() => this.unwatch());
  }

  /** "Germany (DE)" · "no region (ZZ)" · "unknown region". */
  readonly countryLabel = computed(() => {
    const r = this.ads.region();
    if (!r) return 'unknown region';
    if (r === NO_REGION) return 'no region (ZZ)';
    return `${countryName(r)} (${r})`;
  });
  /** "German (de)" · "any language". */
  readonly languageLabel = computed(() => {
    const l = this.ads.language();
    return l ? `${languageName(l)} (${l})` : 'any language';
  });
  readonly sourceLabel = computed(() => {
    switch (this.ads.effectiveMode()) {
      case 'manual':
        return 'country chosen by you';
      case 'off':
        return 'region switched off';
      default: {
        const d = this.ads.detected();
        return d.source === 'timezone'
          ? 'from this device’s time zone'
          : d.source === 'language'
            ? 'from this device’s language'
            : 'nothing could be derived on this device';
      }
    }
  });

  /** Internal ads may link into the app ("app:upgrade"); everything else is https (server-enforced). */
  isAppLink(ad: AdCreative): boolean {
    return ad.target_url.startsWith('app:');
  }
  safeHref(ad: AdCreative): string {
    if (this.isAppLink(ad)) return '#';
    return /^https:\/\//i.test(ad.target_url) ? ad.target_url : '#';
  }
  onLinkClick(ev: MouseEvent, ad: AdCreative) {
    this.ads.click(ad);
    if (this.isAppLink(ad)) {
      ev.preventDefault();
      if (ad.target_url === 'app:upgrade') this.upgrade.emit();
    }
  }

  private watch(ad: AdCreative, el: HTMLElement) {
    this.watching = ad;
    if (typeof IntersectionObserver === 'undefined') {
      // No observer (very old engine / test DOM): count after the dwell time if the tab is visible.
      this.arm(ad);
      return;
    }
    this.observer = new IntersectionObserver(
      (entries) => {
        const e = entries[0];
        if (e && e.isIntersecting && e.intersectionRatio >= VIEWABLE_RATIO) this.arm(ad);
        else this.disarm();
      },
      { threshold: [0, VIEWABLE_RATIO, 1] },
    );
    this.observer.observe(el);
  }

  private arm(ad: AdCreative) {
    if (this.timer) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      if (this.watching === ad && !document.hidden) this.ads.impression(ad);
    }, VIEWABLE_MS);
  }

  private disarm() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }

  private unwatch() {
    this.disarm();
    this.observer?.disconnect();
    this.observer = null;
    this.watching = null;
  }
}
