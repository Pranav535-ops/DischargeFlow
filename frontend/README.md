# DischargeFlow frontend

React 19 + Vite, Tailwind with shadcn/ui components, TanStack Query for server state, React Router for one URL per
screen. Requires Node 22.

```bash
npm ci
npm run dev          # http://localhost:3000, proxies /api to http://localhost:8000
npm test             # Vitest + Testing Library
npx eslint .
npm run build        # -> build/
npx playwright test  # end-to-end; starts the backend and a preview server (see playwright.config.js)
```

Conventions are in the repository's `CLAUDE.md` (Frontend architecture).
