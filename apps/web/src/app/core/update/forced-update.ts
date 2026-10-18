import { Component, ElementRef, effect, inject, viewChild } from '@angular/core';
import { TipDirective } from '../ui/tip.directive';
import { UpdateService } from './update.service';

/**
 * Gate shown for critical releases / unsupported versions until the update is active. It is a
 * modal <dialog> (browser top layer, opened last → drawn above any other dialog) that cannot be
 * dismissed with Escape or a backdrop click.
 */
@Component({
  selector: 'app-forced-update',
  imports: [TipDirective],
  template: `
    <dialog
      #gate
      class="forced-dialog"
      aria-labelledby="forced-title"
      aria-describedby="forced-text"
      (cancel)="$event.preventDefault()"
    >
      <div class="forced-card">
        <h2 id="forced-title">Update required</h2>
        <p id="forced-text">
          {{ u.decision().reason }} Your data is safe on this device; the app just needs the new
          version to continue.
        </p>
        @if (u.swPhase() === 'unsupported') {
          <p class="muted small">
            This browser cannot update in the background. Reload the page to load the newest
            version.
          </p>
          <button
            class="btn primary"
            type="button"
            (click)="reload()"
            appTip="Reload the page to load the newest version."
          >
            Reload now
          </button>
        } @else if (u.decision().ready) {
          <button
            class="btn primary"
            type="button"
            (click)="u.applyNow()"
            appTip="Install the downloaded version and reload."
          >
            Install and reload
          </button>
        } @else if (u.error()) {
          <p class="callout error">{{ u.error() }}</p>
          <button
            class="btn primary"
            type="button"
            (click)="u.checkNow()"
            appTip="Try downloading the update again."
          >
            Try again
          </button>
          <button
            class="btn"
            type="button"
            (click)="reload()"
            appTip="Reload the page to fetch the newest version directly."
          >
            Reload page
          </button>
        } @else {
          <div class="progress indeterminate" role="progressbar">
            <div class="progress-bar" style="width:100%"></div>
          </div>
          <p class="muted small">
            {{
              u.online()
                ? 'Downloading the new version…'
                : "You're offline. The update downloads as soon as you're back online."
            }}
          </p>
          <button
            class="btn"
            type="button"
            (click)="reload()"
            appTip="Reload the page to fetch the newest version directly."
          >
            Reload page
          </button>
        }
      </div>
    </dialog>
  `,
})
export class ForcedUpdate {
  readonly u = inject(UpdateService);
  private readonly gate = viewChild.required<ElementRef<HTMLDialogElement>>('gate');

  constructor() {
    effect(() => {
      const required = this.u.decision().required;
      const d = this.gate().nativeElement;
      if (required && !d.open) d.showModal();
      else if (!required && d.open) d.close();
    });
  }

  reload() {
    location.reload();
  }
}
