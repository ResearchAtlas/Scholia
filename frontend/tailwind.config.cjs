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
