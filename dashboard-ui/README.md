# Automation Control Center (frontend)

Modern dashboard for the Automation Framework backend.

- React 18 + TypeScript + Vite
- Tailwind CSS with a glass / dark theme
- Shadcn-style primitives over Radix UI
- Framer Motion for transitions
- Recharts for trend charts
- Zustand for state, with a single shared poll loop
- `cmdk` command palette + keyboard shortcuts
- `sonner` toast notifications

## Build

```bash
cd dashboard-ui
npm install
npm run build
```

The build outputs straight into the python package, so the backend serves
the new dashboard immediately:

```
src/automation/dashboard/
├── index.html             # entry served by FastAPI at /dashboard
└── static/                # mounted at /dashboard/static
    ├── assets/
    └── index.html
```

The build is hash-routed (`/dashboard/#/accounts`) so reloads on deep
links don't require a server-side SPA fallback — the existing FastAPI
mount works as-is.

## Develop

```bash
npm run dev
```

Vite proxies the API routes (`/status`, `/accounts`, `/workflows`, ...) to
`http://127.0.0.1:8080`, so you can run the backend and the dev server
side-by-side.

## Keyboard shortcuts

| Shortcut          | Action                                  |
|-------------------|------------------------------------------|
| `⌘`/`Ctrl` + `K`  | Open the command palette                 |
| `/`               | Open the command palette (search)        |
| `Esc`             | Close palette / dialogs                  |

## Backend compatibility

The dashboard never modifies backend APIs — every fetch goes through the
typed client in `src/lib/api.ts`. To add a new feature, add the typed
endpoint helper there, then call it from a page.

## Auth

If `AUTOMATION_API_TOKEN` is set on the backend, paste the token via the
key icon in the top bar. It's persisted in `localStorage` and sent as
`Authorization: Bearer <token>` on every request.
