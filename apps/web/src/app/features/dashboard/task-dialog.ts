import { Component, computed, inject, output, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { TaskLimitError, TasksService, isActive } from '../../core/data/tasks.service';
import type { Priority, Task, TaskStatus } from '../../core/db/models';
import { MembershipService } from '../../core/membership/membership.service';
import { DialogDirective } from '../../core/ui/dialog.directive';
import { isCoarsePointer, isoToLocalInput, localInputToIso } from '../../core/ui/format';
import { TipDirective } from '../../core/ui/tip.directive';
import { ToastService } from '../../core/ui/toast.service';

@Component({
  selector: 'app-task-dialog',
  imports: [FormsModule, DialogDirective, TipDirective],
  template: `
    <dialog appDialog id="task-dialog" aria-labelledby="task-dialog-title">
      <form (ngSubmit)="save()" #f="ngForm">
        <h3 id="task-dialog-title">{{ editing() ? 'Edit task' : 'New task' }}</h3>
        @if (limitNotice(); as notice) {
          <div class="callout" id="task-limit-notice" role="status">
            <p>
              <strong>{{ notice.title }}</strong>
            </p>
            <p>{{ notice.text }}</p>
            @if (membership.canUpgrade()) {
              <div class="dialog-actions">
                <button
                  class="btn primary"
                  type="button"
                  id="btn-task-upgrade"
                  (click)="goUpgrade()"
                  appTip="Open the account screen to start a Pro trial or subscribe — unlimited active tasks and encrypted sync."
                >
                  Upgrade to Pro
                </button>
              </div>
            }
          </div>
        }
        <div class="field">
          <label for="task-title">Title</label>
          <input
            class="input"
            id="task-title"
            name="title"
            [(ngModel)]="title"
            required
            maxlength="500"
            autocomplete="off"
            placeholder="What needs doing?"
            appTip="A short name for the task (required)."
          />
        </div>
        <div class="field">
          <label for="task-description">Description <span class="sr-only">(optional)</span></label>
          <textarea
            class="input"
            id="task-description"
            name="description"
            [(ngModel)]="description"
            maxlength="5000"
            rows="2"
            placeholder="Optional"
            appTip="Optional details, links or notes about the task."
          ></textarea>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="task-priority">Priority</label>
            <select
              class="input"
              id="task-priority"
              name="priority"
              [(ngModel)]="priority"
              appTip="How important the task is. Urgent and high-priority tasks are listed first."
            >
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
              <option value="urgent">Urgent</option>
            </select>
          </div>
          <div class="field">
            <label for="task-status">Status</label>
            <select
              class="input"
              id="task-status"
              name="status"
              [ngModel]="status()"
              (ngModelChange)="status.set($event)"
              appTip="Where the task stands: To do, In progress or Done."
            >
              <option value="todo">To do</option>
              <option value="in_progress">In progress</option>
              <option value="done">Done</option>
            </select>
          </div>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="task-due">Due</label>
            <input
              class="input"
              id="task-due"
              name="due"
              type="datetime-local"
              [(ngModel)]="due"
              appTip="When the task should be finished (your local time). Overdue tasks are highlighted in red. Leave empty for no deadline."
            />
          </div>
          <div class="field">
            <label for="task-tags">Tags</label>
            <input
              class="input"
              id="task-tags"
              name="tags"
              [(ngModel)]="tags"
              placeholder="home, work"
              autocomplete="off"
              appTip="Optional labels, separated by commas (e.g. home, work)."
            />
          </div>
        </div>
        <div class="dialog-actions">
          @if (editing()) {
            <button
              class="btn danger"
              type="button"
              (click)="remove()"
              appTip="Delete this task permanently (you will be asked to confirm)."
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
            [disabled]="busy() || !!limitNotice()"
            appTip="Save the task on this device (and sync it if you use an account)."
          >
            Save
          </button>
        </div>
      </form>
    </dialog>
  `,
})
export class TaskDialog {
  private readonly tasks = inject(TasksService);
  private readonly toast = inject(ToastService);
  readonly membership = inject(MembershipService);
  readonly dialog = viewChild.required(DialogDirective);
  /** The user wants to upgrade (the app opens the account screen). */
  readonly upgrade = output<void>();

  readonly editing = signal<Task | null>(null);
  readonly busy = signal(false);
  title = '';
  description = '';
  priority: Priority = 'medium';
  readonly status = signal<TaskStatus>('todo');
  due = '';
  tags = '';

  /**
   * Shown when saving would open one more active task than the membership allows: a new task
   * (unless created as Done) or re-opening a done task. Mirrors TasksService.assertCanActivate.
   */
  readonly limitNotice = computed(() => {
    const check = this.tasks.limit();
    const editing = this.editing();
    const activates = editing
      ? !isActive(editing) && isActive({ status: this.status() })
      : isActive({ status: this.status() });
    if (check.allowed || !activates) return null;
    return {
      title: `Active task limit reached (${check.limit})`,
      text:
        check.reason ??
        `You already have ${check.limit} active tasks. Finish or delete one to add another.`,
    };
  });

  open(task: Task | null = null): void {
    this.editing.set(task);
    this.title = task?.title ?? '';
    this.description = task?.description ?? '';
    this.priority = task?.priority ?? 'medium';
    this.status.set(task?.status ?? 'todo');
    this.due = isoToLocalInput(task?.dueAt ?? null);
    this.tags = (task?.tags ?? []).join(', ');
    this.dialog().open();
    if (!isCoarsePointer()) setTimeout(() => document.getElementById('task-title')?.focus(), 0);
  }

  goUpgrade(): void {
    this.dialog().close();
    this.upgrade.emit();
  }

  async save(): Promise<void> {
    this.busy.set(true);
    try {
      const input = {
        title: this.title,
        description: this.description || null,
        priority: this.priority,
        status: this.status(),
        dueAt: localInputToIso(this.due),
        tags: this.tags
          .split(',')
          .map((t) => t.trim())
          .filter(Boolean),
      };
      const editing = this.editing();
      if (editing) await this.tasks.update(editing, input);
      else await this.tasks.create(input);
      this.toast.show(editing ? 'Task updated' : 'Task added');
      this.dialog().close();
    } catch (e) {
      if (e instanceof TaskLimitError)
        this.toast.error(e.message); // also shown in the notice above
      else this.toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      this.busy.set(false);
    }
  }

  async remove(): Promise<void> {
    const task = this.editing();
    if (!task || !confirm(`Delete "${task.title}"?`)) return;
    await this.tasks.remove(task);
    this.toast.show('Task deleted');
    this.dialog().close();
  }
}
