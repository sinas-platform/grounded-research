/** @type {import('tailwindcss').Config} */
// Palette follows the Sinas console light theme: warm off-white page,
// white cards, and the Sinas brand orange (Tailwind orange scale) as the
// single accent — replacing the old forest green.
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        page: '#f7f7f5',
        primary: {
          50: '#fff7ed',
          100: '#ffedd5',
          200: '#fed7aa',
          300: '#fdba74',
          400: '#fb923c',
          500: '#f97316',
          600: '#ea580c',
          700: '#c2410c',
          800: '#9a3412',
          900: '#7c2d12',
        },
      },
    },
  },
  plugins: [],
};
