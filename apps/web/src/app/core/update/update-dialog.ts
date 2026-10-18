import { Component, computed, inject, viewChild } from '@angular/core';
import { DialogDirective } from '../ui/dialog.directive';
import { TipDirective } from '../ui/tip.directive';
import { ToastService } from '../ui/toast.service';
import { relative } from './update-policy';
import { UpdateService } from './update.service';

@Component({
  selector: 'app-update-dialog',
  imports: [DialogDirective, TipDirective],
  template: `
    <dialog appDialog id="update-dialog" aria-labelledby="update-dialog-title">
      <form (submit)="$event.preventDefault()">
        <h3 id="update-dialog-title">Update Personal Assistant</h3>
        <p class="muted small">
          New versions are published to the cloud. This app checks for them when you open it and
          every few hours, downloads them in the background and — by default — installs them as soon
          as you are not in the middle of something. Your data stays on this device.
        </p>

        <div class="update-summary">
          <div>
            <span class="muted">Installed version</span><strong>v{{ u.currentVersion }}</strong>
          </div>
          <div>
            <span class="muted">Latest version</span><strong>{{ latestLabel() }}</strong>
          </div>
          <div>
            <span class="muted">Last checked</span
            ><span>{{ u.lastChecked() ? rel(u.lastChecked()!) : 'never' }}</span>
          </div>
          <div>
            <span class="muted">Data format</span><span>schema {{ u.currentSchema }}</span>
          </div>
        </div>

        @if (status(); as s) {
          <div class="update-status">
            <div
              class="progress"
              [class.indeterminate]="s.busy"
              role="progressbar"
              aria-valuemin="0"
              aria-valuemax="100"
            >
              <div class="progress-bar" [style.width.%]="s.busy ? 100 : s.done ? 100 : 0"></div>
            </div>
            <p>{{ s.text }}</p>
          </div>
        }
        @if (u.error(); as err) {
          <div class="callout error" role="alert">
            <strong>Update failed</strong>
            <p>{{ err }}</p>
          </div>
        }
        @if (u.manifestError(); as err) {
          <div class="callout error" role="alert">
            <strong>Couldn't check for updates</strong>
            <p>{{ err }}</p>
          </div>
        }
        @if (u.decision().required) {
          <div class="callout error" role="alert">
            <strong>Update required</strong>
            <p>{{ u.decision().reason }}</p>
          </div>
        }

        <label class="checkbox" for="update-auto">
          <input
            type="checkbox"
            id="update-auto"
            [checked]="autoUpdate()"
            (change)="setAuto($any($event.target).checked)"
            appTip="When on (the default), new versions install themselves as soon as they are downloaded and you are not busy — and whenever you reopen the app. Turn it off to install only when you tap Install. Required (critical) updates install regardless."
          />
          <span>Install new versions automatically</span>
        </label>

        @if (u.manifest()?.notes; as notes) {
          <div class="field">
            <span class="muted small">What's new in v{{ u.manifest()!.version }}</span>
            <p class="release-notes muted small">{{ notes }}</p>
          </div>
        }
        <p class="muted small">
          {{
            u.swPhase() === 'unsupported'
              ? 'This browser does not support background updates; reload the page to get the newest version.'
              : 'Updates are downloaded by the service worker and verified before they are installed.'
          }}
        </p>

        <div class="dialog-actions">
          <button
            class="btn"
            type="button"
            (click)="check()"
            [disabled]="!u.online()"
            appTip="Look for a new version right now."
          >
            Check now
          </button>
          <span class="spacer"></span>
          <button
            class="btn"
            type="button"
            (click)="dialog().close()"
            appTip="Close this window. Downloads continue in the background."
          >
            Close
          </button>
          <button
            class="btn primary"
            type="button"
            (click)="install()"
            [disabled]="!canInstall()"
            appTip="Install the downloaded version now. The app reloads; nothing you've saved is lost."
          >
            {{ installLabel() }}
          </button>
        </div>
      </form>
    </dialog>
  `,
})
export class UpdateDialog {
  readonly u = inject(UpdateService);
  private readonly toast = inject(ToastService);
  readonly dialog = viewChild.required(DialogDirective);
  readonly rel = relative;

  readonly autoUpdate = this.u.autoUpdate;
  readonly latestLabel = computed(() => {
    const d = this.u.decision();
    if (d.availableVersion)
      return `v${d.availableVersion}${d.required ? ' — required' : ' — new!'}`;
    return this.u.manifest() ? `v${this.u.manifest()!.version}` : 'unknown';
  });
  readonly status = computed(() => {
    const phase = this.u.swPhase();
    const d = this.u.decision();
    if (phase === 'downloading')
      return { busy: true, done: false, text: `Downloading v${d.availableVersion ?? ''}…` };
    if (phase === 'checking') return { busy: true, done: false, text: 'Checking for updates…' };
    if (phase === 'activating')
      return { busy: true, done: false, text: 'Installing… the app reloads in a moment.' };
    if (phase === 'ready')
      return {
        busy: false,
        done: true,
        text: `v${d.availableVersion ?? ''} is downloaded and ready to install.`,
      };
    return null;
  });
  readonly canInstall = computed(
    () => this.u.decision().ready || this.u.swPhase() === 'unsupported',
  );
  readonly installLabel = computed(() => {
    const d = this.u.decision();
    if (this.u.swPhase() === 'unsupported') return 'Reload';
    return d.availableVersion ? `Install v${d.availableVersion}` : 'Install';
  });

  open() {
    this.dialog().open();
    void this.u.checkNow();
  }

  async check() {
    await this.u.checkNow();
    const d = this.u.decision();
    if (this.u.manifestError()) this.toast.error(this.u.manifestError()!);
    else if (d.availableVersion) this.toast.show(`Update available: v${d.availableVersion}`);
    else this.toast.show("You're up to date");
  }

  install() {
    if (this.u.swPhase() === 'unsupported') location.reload();
    else void this.u.applyNow();
  }

  setAuto(on: boolean) {
    this.u.setAutoUpdate(on);
    this.toast.show(
      on
        ? 'New versions will be installed automatically'
        : 'Automatic updates are off — use the Update button to install them',
    );
  }
}
