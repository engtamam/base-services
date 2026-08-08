import { forwardRef } from 'react';
import type { ButtonHTMLAttributes } from 'react';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '../lib/cn';

// cva primitive (adapted from the Owner Hub ui-kit) — classes reference theme token utilities only.
export const buttonVariants = cva(
  'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md font-medium ' +
    'transition-colors outline-none focus-ring disabled:pointer-events-none disabled:opacity-50',
  {
    variants: {
      variant: {
        solid: 'bg-accent text-accent-contrast shadow-sm transition-all hover:bg-accent-hover hover:shadow-glow',
        soft: 'bg-accent-soft text-accent-on-soft hover:opacity-90',
        outline: 'border border-border-strong bg-transparent text-text-primary hover:bg-surface-2',
        ghost: 'bg-transparent text-text-secondary hover:bg-surface-2 hover:text-text-primary',
        danger: 'bg-danger text-danger-contrast hover:opacity-90',
      },
      size: {
        sm: 'h-8 px-3 text-sm',
        md: 'h-10 px-4 text-sm',
        lg: 'h-11 px-6 text-base',
        icon: 'h-10 w-10 p-0',
      },
    },
    defaultVariants: { variant: 'solid', size: 'md' },
  },
);

export interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, ...props }, ref) => (
    <button ref={ref} className={cn(buttonVariants({ variant, size }), className)} {...props} />
  ),
);
Button.displayName = 'Button';
