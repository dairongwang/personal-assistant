import { Directive, ElementRef, inject, output } from '@angular/core';
import { UiStateService } from './ui-state.service';

/**
 * Behaviour shared by every `<dialog>`: backdrop click closes, open/close is reported to
 * UiStateService (so updates never reload the page while a dialog is open).
 */
@Directive({
  selector: 'dialog[appDialog]',
  host: { '(click)': 'onClick($event)', '(close)': 'onClose()' },
})
export class DialogDirective {
  private readonly el = inject<ElementRef<HTMLDialogElement>>(ElementRef);
  private readonly ui = inject(UiStateService);
  readonly closed = output<void>();

  open(): void {
    const d = this.el.nativeElement;
    if (!d.open) {
      d.showModal();
      this.ui.dialogOpened();
    }
  }

  close(): void {
    if (this.el.nativeElement.open) this.el.nativeElement.close();
  }

  get isOpen() {
    return this.el.nativeElement.open;
  }

  onClick(e: MouseEvent) {
    if (e.target === this.el.nativeElement) this.close();
  }

  onClose() {
    this.ui.dialogClosed();
    this.closed.emit();
  }
}
