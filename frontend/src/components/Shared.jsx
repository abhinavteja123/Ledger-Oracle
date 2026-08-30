import { useEffect, useRef, useState } from "react";

export function useReveal() {
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) {
            el.classList.add("in");
            io.unobserve(el);
          }
        });
      },
      { threshold: 0.12 }
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);
  return ref;
}

export function Reveal({ as: Tag = "div", className = "", children, ...rest }) {
  const ref = useReveal();
  return (
    <Tag ref={ref} className={"reveal " + className} {...rest}>
      {children}
    </Tag>
  );
}

export function useTheme() {
  const [theme, setTheme] = useState(() => localStorage.getItem("lo-theme") || "");
  useEffect(() => {
    if (theme) document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);
  const toggle = () => {
    const current = theme || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
    const next = current === "dark" ? "light" : "dark";
    localStorage.setItem("lo-theme", next);
    setTheme(next);
  };
  return { theme, toggle };
}

export function ThemeToggle() {
  const { toggle } = useTheme();
  return (
    <button className="theme-btn" onClick={toggle} aria-label="Toggle light/dark theme">
      Night / Day
    </button>
  );
}

export function Mesh() {
  return (
    <div className="oracle-mesh">
      <span className="b1"></span>
      <span className="b2"></span>
      <span className="b3"></span>
    </div>
  );
}
