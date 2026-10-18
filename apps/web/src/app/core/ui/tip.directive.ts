import { Directive, ElementRef, effect, inject, input } from '@angular/core';
import { TooltipService } from './tooltip.service';

/**
 * `<button appTip="What this does">` — attaches an explanatory tooltip to any control.
 * Hover/focus on desktop, press-and-hold or Help mode on touch devices.
 */
@Directive({ selector: '[appTip]' })
export class TipDirective {
  readonly appTip = input.required<string>();
  private readonly el = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly tooltips = inject(TooltipService);

  constructor() {
    effect(() => {
      const text = this.appTip();
      this.el.nativeElement.setAttribute('data-tip', text);
      this.tooltips.refresh(this.el.nativeElement);
    });
  }
}
