// 按钮组件：variant + size，参考 shadcn/ui
import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-md text-[13px] font-semibold transition-all focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/45 disabled:pointer-events-none disabled:opacity-45 active:scale-[0.975]",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground shadow-sm hover:bg-primary-hover",
        destructive:
          "bg-destructive text-destructive-foreground shadow-sm hover:bg-destructive-hover",
        softDestructive: "bg-destructive/15 text-destructive hover:bg-destructive/25",
        outline:
          "border border-border bg-card hover:bg-accent hover:text-accent-foreground",
        secondary: "bg-secondary text-secondary-foreground hover:bg-secondary-hover",
        tinted: "bg-primary/10 text-primary hover:bg-primary/[0.18]",
        quiet: "bg-muted text-foreground hover:bg-accent",
        ghost: "hover:bg-accent hover:text-accent-foreground",
        link: "bg-primary/10 text-primary hover:bg-primary/[0.18]",
      },
      size: {
        default: "h-[34px] px-4",
        sm: "h-8 rounded-[7px] px-3 text-xs",
        lg: "h-10 rounded-[9px] px-5 text-sm",
        icon: "h-[34px] w-[34px]",
      },
    },
    compoundVariants: [
      { variant: "link", class: "h-auto rounded-sm px-2.5 py-1" },
    ],
    defaultVariants: { variant: "default", size: "default" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        ref={ref}
        className={cn(buttonVariants({ variant, size, className }))}
        {...props}
      />
    );
  },
);
Button.displayName = "Button";
export { buttonVariants };
