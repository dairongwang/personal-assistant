import { Component, inject, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { EventsService } from '../../core/data/events.service';
import type { CalendarEvent } from '../../core/db/models';
import { DialogDirective } from '../../core/ui/dialog.directive';
import { isCoarsePointer, isoToLocalInput, localInputToIso } from '../../core/ui/format';
import { TipDirective } from '../../core/ui/tip.directive';
import { ToastService } from '../../core/ui/toast.service';

@Component({
  selector: 'app-event-dialog',
  imports: [FormsModule, DialogDirective, TipDirective],
  template: `
    <dialog appDialog id="event-dialog" aria-labelledby="event-dialog-title">
      <form (ngSubmit)="save()">
        <h3 id="event-dialog-title">{{ editing() ? 'Edit event' : 'New event' }}</h3>
        <div class="field">
          <label for="event-title">Title</label>
          <input
            class="input"
            id="event-title"
            name="title"
            [(ngModel)]="title"
            required
            maxlength="500"
            autocomplete="off"
            appTip="What the event is (required)."
          />
        </div>
        <div class="field-row">
          <div class="field">
            <label for="event-start">Starts</label>
            <input
              class="input"
              id="event-start"
              name="start"
              type="datetime-local"
              [(ngModel)]="start"
              (change)="keepDuration()"
              required
              appTip="When the event begins (your local time)."
            />
          </div>
          <div class="field">
            <label for="event-end">Ends</label>
            <input
              class="input"
              id="event-end"
              name="end"
              type="datetime-local"
              [(ngModel)]="end"
              required
              appTip="When the event ends. It is moved automatically if you change the start."
            />
          </div>
        </div>
        <div class="field">
          <label for="event-location">Location <span class="sr-only">(optional)</span></label>
          <input
            class="input"
            id="event-location"
            name="location"
            [(ngModel)]="location"
            maxlength="500"
            placeholder="Optional"
            autocomplete="off"
            appTip="Where the event takes place (optional)."
          />
        </div>
        <div class="field">
          <label for="event-description">Notes <span class="sr-only">(optional)</span></label>
          <textarea
            class="input"
            id="event-description"
            name="description"
            [(ngModel)]="description"
            maxlength="5000"
            rows="2"
            placeholder="Optional"
            appTip="Optional notes for the event."
          ></textarea>
        </div>
        <div class="dialog-actions">
          @if (editing()) {
            <button
              class="btn danger"
              type="button"
              (click)="remove()"
              appTip="Delete this event permanently (you will be asked to confirm)."
            >
              Delete
            </button>
          }
          <span class="spacer"></span>
          <button
            class="btn"
            type="button"
            (click)="dialog().close()"
            appTip="Close without saving changes."
          >
            Cancel
          </button>
          <button
            class="btn primary"
            type="submit"
            [disabled]="busy()"
            appTip="Save the event on this device (and sync it if you use an account)."
          >
            Save
          </button>
        </div>
      </form>
    </dialog>
  `,
})
export class EventDialog {
  private readonly events = inject(EventsService);
  private readonly toast = inject(ToastService);
  readonly dialog = viewChild.required(DialogDirective);
  readonly editing = signal<CalendarEvent | null>(null);
  readonly busy = signal(false);
  title = '';
  start = '';
  end = '';
  location = '';
  description = '';

  open(event: CalendarEvent | null = null): void {
    this.editing.set(event);
    if (event) {
      this.title = event.title;
      this.start = isoToLocalInput(event.startAt);
      this.end = isoToLocalInput(event.endAt);
      this.location = event.location ?? '';
      this.description = event.description ?? '';
    } else {
      const s = new Date();
      s.setMinutes(0, 0, 0);
      s.setHours(s.getHours() + 1);
      const e = new Date(s);
      e.setHours(e.getHours() + 1);
      this.title = this.location = this.description = '';
      this.start = isoToLocalInput(s.toISOString());
      this.end = isoToLocalInput(e.toISOString());
    }
    this.dialog().open();
    if (!isCoarsePointer()) setTimeout(() => document.getElementById('event-title')?.focus(), 0);
  }

  keepDuration(): void {
    const s = new Date(this.start),
      e = new Date(this.end);
    if (!isNaN(+s) && (isNaN(+e) || e <= s)) {
      const n = new Date(s);
      n.setHours(n.getHours() + 1);
      this.end = isoToLocalInput(n.toISOString());
    }
  }

  async save(): Promise<void> {
    this.busy.set(true);
    try {
      const input = {
        title: this.title,
        startAt: localInputToIso(this.start) ?? '',
        endAt: localInputToIso(this.end) ?? '',
        location: this.location || null,
        description: this.description || null,
      };
      const editing = this.editing();
      if (editing) await this.events.update(editing, input);
      else await this.events.create(input);
      this.toast.show(editing ? 'Event updated' : 'Event added');
      this.dialog().close();
    } catch (e) {
      this.toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      this.busy.set(false);
    }
  }

  async remove(): Promise<void> {
    const ev = this.editing();
    if (!ev || !confirm(`Delete "${ev.title}"?`)) return;
    await this.events.remove(ev);
    this.toast.show('Event deleted');
    this.dialog().close();
  }
}
