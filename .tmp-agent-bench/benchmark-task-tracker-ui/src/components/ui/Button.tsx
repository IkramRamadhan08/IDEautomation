import type { ButtonHTMLAttributes, ReactNode } from "react";

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost";
  children: ReactNode;
};

export default function Button(props: ButtonProps) {
  const { variant = "primary", children, className = "", type = "button", ...buttonProps } = props;
  const classes = ["btn", variant === "ghost" ? "btnGhost" : "btnPrimary", className].filter(Boolean).join(" ");
  return (
    <button {...buttonProps} type={type} className={classes}>
      {children}
    </button>
  );
}
