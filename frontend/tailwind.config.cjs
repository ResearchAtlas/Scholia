// Carried from the current app: frontend/tailwind.config.cjs at b5d687820e88c10de25a9a2343d3cc478e497524,
// with round 2 of the product prototype's additions (slice-1 spec section 3): the indigo brand
// accent, the sidebar surface, success and warning, and the system and serif faces.
/** @type {import('tailwindcss').Config} */
module.exports = {
    darkMode: ["class"],
    content: [
        "./index.html",
        "./src/**/*.{js,ts,jsx,tsx}",
    ],
    theme: {
        extend: {
            fontFamily: {
                sans: ["-apple-system", "BlinkMacSystemFont", '"SF Pro Text"', '"PingFang SC"', '"Helvetica Neue"', "sans-serif"],
                serif: ["ui-serif", '"Iowan Old Style"', "Georgia", '"Songti SC"', "serif"],
                mono: ["ui-monospace", '"SF Mono"', "Menlo", "monospace"],
            },
            colors: {
                border: "hsl(var(--border))",
                input: "hsl(var(--input))",
                ring: "hsl(var(--ring))",
                background: "hsl(var(--background))",
                foreground: "hsl(var(--foreground))",
                sidebar: "hsl(var(--sidebar))",
                brand: {
                    DEFAULT: "hsl(var(--brand))",
                    foreground: "hsl(var(--brand-foreground))",
                    soft: "hsl(var(--brand-soft))",
                },
                primary: {
                    DEFAULT: "hsl(var(--primary))",
                    foreground: "hsl(var(--primary-foreground))",
                },
                secondary: {
                    DEFAULT: "hsl(var(--secondary))",
                    foreground: "hsl(var(--secondary-foreground))",
                },
                destructive: {
                    DEFAULT: "hsl(var(--destructive))",
                    foreground: "hsl(var(--destructive-foreground))",
                },
                muted: {
                    DEFAULT: "hsl(var(--muted))",
                    foreground: "hsl(var(--muted-foreground))",
                },
                accent: {
                    DEFAULT: "hsl(var(--accent))",
                    foreground: "hsl(var(--accent-foreground))",
                },
                popover: {
                    DEFAULT: "hsl(var(--popover))",
                    foreground: "hsl(var(--popover-foreground))",
                },
                card: {
                    DEFAULT: "hsl(var(--card))",
                    foreground: "hsl(var(--card-foreground))",
                },
                success: "hsl(var(--success))",
                warning: "hsl(var(--warning))",
                // Tailwind 3's values of the two palette colors in use (the mark's gradient and the
                // placeholder default), which Tailwind 4's palette changed (S1-35).
                violet: { 500: "#8b5cf6" },
                gray: { 400: "#9ca3af" },
            },
            // Tailwind 3's type scale, whose line heights are lengths: Tailwind 4's are ratios, which
            // text of another size inside would inherit and scale (S1-35).
            fontSize: {
                xs: ["0.75rem", { lineHeight: "1rem" }],
                sm: ["0.875rem", { lineHeight: "1.25rem" }],
                base: ["1rem", { lineHeight: "1.5rem" }],
                lg: ["1.125rem", { lineHeight: "1.75rem" }],
                xl: ["1.25rem", { lineHeight: "1.75rem" }],
                "2xl": ["1.5rem", { lineHeight: "2rem" }],
                "3xl": ["1.875rem", { lineHeight: "2.25rem" }],
                "4xl": ["2.25rem", { lineHeight: "2.5rem" }],
            },
            // Tailwind 3's color transition, which leaves the focus outline's color out: Tailwind 4's
            // fades the outline in (S1-35).
            transitionProperty: {
                colors: "color, background-color, border-color, text-decoration-color, fill, stroke",
            },
            borderRadius: {
                lg: "var(--radius)",
                md: "calc(var(--radius) - 2px)",
                sm: "calc(var(--radius) - 4px)",
            },
            keyframes: {
                "fade-up": { from: { opacity: "0", transform: "translateY(4px)" }, to: { opacity: "1", transform: "none" } },
            },
            animation: {
                "fade-up": "fade-up .2s ease-out",
            },
        },
    },
    plugins: [require("tailwindcss-animate")],
}
