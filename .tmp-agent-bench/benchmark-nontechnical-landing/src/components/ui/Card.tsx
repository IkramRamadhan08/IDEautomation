import type { ComponentPropsWithoutRef, ReactNode } from "react";

type CardProps = ComponentPropsWithoutRef<"section"> & {
  title?: string;
  eyebrow?: string;
  children: ReactNode;
};

export default function Card(props: CardProps) {
  const { title, eyebrow, children, className = "", ...sectionProps } = props;
  const classes = ["card", className].filter(Boolean).join(" ");
  return (
    <section {...sectionProps} className={classes}>
      {eyebrow ? <div className="eyebrow">{eyebrow}</div> : null}
      {title ? <h2 className="cardTitle">{title}</h2> : null}
      <div className="cardBody">{children}</div>
    </section>
  );
}
