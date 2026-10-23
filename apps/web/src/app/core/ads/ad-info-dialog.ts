import { Component, computed, inject, output, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MembershipService } from '../membership/membership.service';
import type { AdRegionMode } from '../settings/settings.service';
import { DialogDirective } from '../ui/dialog.directive';
import { TipDirective } from '../ui/tip.directive';
import { AdsService } from './ads.service';
import { countryName, countryOptions } from './region';

/**
 * "Why am I seeing this?" — the transparency and control screen for sponsored content.
 * Plain language, the actual values in use on this device, and the switches (GDPR Art. 13/21,
 * CCPA notice-at-collection): turn it off, choose the region, or go Pro.
 */
@Component({
  selector: 'app-ad-info-dialog',
  imports: [FormsModule, DialogDirective, TipDirective],
  template: `
    <dialog appDialog id="ad-info-dialog" aria-labelledby="ad-info-title">
      <form (ngSubmit)="dialog().close()">
        <h3 id="ad-info-title">About sponsored content</h3>
        <p class="muted small">
          The free version shows one small, clearly labelled sponsored message. It is picked by
          context only — your country and language as known to this device — never by who you are or
          what you write. There is no ad network, no cookie and no tracker involved.
        </p>

        <div class="update-summary" id="ad-info-facts">
          <div id="ad-info-source">
            <span class="muted">Source</span>
            <strong
              >{{ ads.source()?.title ?? 'not loaded yet'
              }}{{ ads.source()?.mode === 'fake' ? ' (fake)' : '' }}</strong
            >
            <span class="small">
              @if (ads.source()?.third_party) {
                Third party: {{ ads.source()?.provider_name }} —
                {{
                  ads.consentState() === 'granted'
                    ? 'allowed on this device'
                    : ads.consentState() === 'denied'
                      ? 'declined on this device'
                      : 'not loaded until you allow it'
                }}.
                @if (ads.consentState()) {
                  <button
                    type="button"
                    class="link"
                    id="ad-info-withdraw"
                    (click)="withdraw()"
                    appTip="Forget your decision for this provider; the panel asks again and loads nothing until then."
                  >
                    Withdraw
                  </button>
                }
              } @else if (ads.source()?.kind === 'network') {
                Served to this server by {{ ads.source()?.provider_name }}; your browser never
                contacts the network.
              } @else {
                First-party: chosen and delivered by this app's own server.
              }
            </span>
          </div>
          <div>
            <span class="muted">Used to pick it</span>
            <strong id="ad-info-region">Region: {{ regionLabel() }}</strong>
            <span class="small"
              >Language: {{ ads.language() ?? 'unknown' }} · Membership:
              {{ membership.label() }}</span
            >
          </div>
          <div>
            <span class="muted">Never used</span>
            <strong>Your tasks, events and notes</strong>
            <span class="small"
              >Nor your account or e-mail, a precise location, browsing history, cookies or
              third-party trackers. The request is sent without your sign-in.</span
            >
          </div>
          <div>
            <span class="muted">What is counted</span>
            <strong>Views and clicks — per message, per day, per country</strong>
            <span class="small">Anonymous totals; nothing that identifies you or this device.</span>
          </div>
          <div>
            <span class="muted">Shared with sponsors</span>
            <strong>Nothing</strong>
            <span class="small"
              >Links open in a new tab without a referrer; sponsors only see their own totals.</span
            >
          </div>
        </div>

        @if (ads.privacySignal) {
          <div class="callout small" id="ad-info-gpc">
            <p>
              Your browser sends a Global Privacy Control / Do Not Track signal, so no region is
              detected automatically. You can still choose one below.
            </p>
          </div>
        }

        <label class="checkbox" for="ad-enabled">
          <input
            type="checkbox"
            id="ad-enabled"
            [checked]="ads.enabled()"
            (change)="ads.setEnabled($any($event.target).checked)"
            appTip="Off: the sponsored panel disappears on this device and nothing is requested. You can turn it back on here at any time."
          />
          <span>Show sponsored content on this device</span>
        </label>

        <div class="field">
          <label for="ad-region-mode">Pick offers for</label>
          <select
            class="input"
            id="ad-region-mode"
            name="mode"
            [ngModel]="ads.regionMode()"
            (ngModelChange)="setMode($event)"
            [disabled]="!ads.enabled()"
            appTip="Which country the offers are for. Automatic uses the time zone and language of this device; nothing is looked up elsewhere. 'No region' shows worldwide offers only."
          >
            <option value="auto">Detected on this device: {{ detectedLabel() }}</option>
            <option value="manual">A country I choose</option>
            <option value="off">No region — worldwide offers only</option>
          </select>
        </div>
        @if (ads.regionMode() === 'manual') {
          <div class="field">
            <label for="ad-region">Country</label>
            <select
              class="input"
              id="ad-region"
              name="region"
              [ngModel]="ads.manualRegion() ?? ''"
              (ngModelChange)="ads.setRegionMode('manual', $event || null)"
              [disabled]="!ads.enabled()"
              appTip="Offers are picked for this country. Saved on this device only."
            >
              <option value="">Choose a country…</option>
              @for (c of countries; track c.code) {
                <option [value]="c.code">{{ c.name }}</option>
              }
            </select>
          </div>
        }

        <label class="checkbox" for="ad-large">
          <input
            type="checkbox"
            id="ad-large"
            [checked]="ads.large()"
            (change)="ads.setLarge($any($event.target).checked)"
            [disabled]="!ads.enabled()"
            appTip="Large panel: about a third of the page, with the country and language it was picked for highlighted (showcase/debug view). Off: the compact card."
          />
          <span>Large panel with targeting highlighted (showcase view)</span>
        </label>

        <p class="muted small">
          Your choices here are saved on this device only. Pro and Lifetime memberships have no
          sponsored content.
        </p>
        <div class="dialog-actions">
          @if (membership.canUpgrade()) {
            <button
              class="btn"
              type="button"
              id="ad-info-upgrade"
              (click)="goPro()"
              appTip="Open the account screen to start a trial or subscribe. Pro has unlimited tasks, encrypted sync and no sponsored content."
            >
              Go Pro — no sponsored content
            </button>
          }
          <span class="spacer"></span>
          <button class="btn primary" type="submit" appTip="Close this window.">Done</button>
        </div>
      </form>
    </dialog>
  `,
})
export class AdInfoDialog {
  readonly ads = inject(AdsService);
  readonly membership = inject(MembershipService);
  readonly dialog = viewChild.required(DialogDirective);
  /** The shell opens the account dialog (trial / subscribe). */
  readonly upgrade = output<void>();

  readonly countries = countryOptions();

  readonly detectedLabel = computed(() => {
    const d = this.ads.detected();
    if (!d.region) return 'no country could be derived';
    return `${countryName(d.region)} (from ${d.source === 'timezone' ? 'time zone' : 'language'})`;
  });

  readonly regionLabel = computed(() => {
    const mode = this.ads.effectiveMode();
    const region = this.ads.region();
    if (!this.ads.enabled()) return 'sponsored content is off';
    if (mode === 'off' || region === 'ZZ') return 'none (worldwide offers only)';
    if (!region) return 'unknown (worldwide offers only)';
    return `${countryName(region)}${mode === 'manual' ? ' (chosen by you)' : ' (detected on this device)'}`;
  });

  open() {
    this.dialog().open();
  }

  setMode(mode: AdRegionMode) {
    this.ads.setRegionMode(mode);
  }

  goPro() {
    this.dialog().close();
    this.upgrade.emit();
  }

  withdraw() {
    const src = this.ads.source();
    if (src) this.ads.withdrawConsent(src.kind);
  }
}
