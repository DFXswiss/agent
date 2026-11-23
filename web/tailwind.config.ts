import type { Config } from 'tailwindcss';

// Mobile first, breakpoints of 06-web.md section 12.1 (sm 640, md 768, lg 1024 px); compiled to a static file.
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    screens: { sm: '640px', md: '768px', lg: '1024px' },
    extend: {},
  },
  plugins: [],
} satisfies Config;
