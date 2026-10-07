/** @type {import('tailwindcss').Config} */
module.exports = {
    content: [
        './templates/**/*.html',
        './apps/**/*.py',
    ],
    theme: {
        extend: {
            fontFamily: {
                display: [
                    '"Inter var"',
                    'Inter',
                    'system-ui',
                    '-apple-system',
                    'BlinkMacSystemFont',
                    '"Segoe UI"',
                    'Roboto',
                    '"Helvetica Neue"',
                    'Arial',
                    'sans-serif',
                ],
                sans: [
                    '"Inter var"',
                    'Inter',
                    'system-ui',
                    '-apple-system',
                    'BlinkMacSystemFont',
                    '"Segoe UI"',
                    'Roboto',
                    '"Helvetica Neue"',
                    'Arial',
                    'sans-serif',
                ],
            },
            colors: {
                flash: {
                    50: '#f5f8ff',
                    100: '#e8eeff',
                    200: '#d6e0ff',
                    300: '#b9ccff',
                    400: '#8eaaff',
                    500: '#5f7bff',
                    600: '#3c52f5',
                    700: '#283ee0',
                    800: '#222fb3',
                    900: '#1f2a8f',
                    950: '#141951',
                },
            },
            boxShadow: {
                soft: '0px 1px 2px rgba(15, 23, 42, 0.06), 0px 1px 3px rgba(15, 23, 42, 0.10)',
                elevated: '0px 10px 40px -20px rgba(15, 23, 42, 0.45)',
            },
            borderRadius: {
                xl: '1rem',
                '2xl': '1.25rem',
            },
        },
    },
    /*
     * Classes defined in frontend/css/tailwind.css are tree-shaken like any utility, so
     * helpers that templates do not reference yet must be safelisted explicitly.
     */
    safelist: [
        'skip-link',
        'htmx-indicator',
        'shadow-elevated',
        'hover-lift',
        'hero-anim-bg',
        'hero-anim-eyebrow',
        'hero-anim-title',
        'hero-anim-desc',
        'hero-anim-cta',
        'hero-anim-pills',
        'hero-anim-visual',
        'js-reveal-active',
    ],
    plugins: [],
};