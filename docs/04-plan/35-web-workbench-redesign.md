# Web workbench redesign

Date: 2026-09-22/23. User brief: rebuild the existing frontend as a distinctive,
refined workbench for daily use; organise by what a person needs to know and do
next, not by API fields; neutral base with a clear brand accent and unified
status colours in light and dark; continuous sections, object lists,
master-detail and in-place actions instead of card walls; adapt mature
component patterns (shadcn/ui, Aceternity, Origin UI); complete every page and
loop including search, detail/back, loading, empty, error and offline states;
responsive, keyboard-operable, reduced-motion aware; verified in a real browser.

## What changed

- Stack: Element Plus removed. Tailwind CSS v4 (`@tailwindcss/vite`), reka-ui
  headless primitives, lucide icons, `tw-animate-css`. Components live in
  `apps/web/src/components/ui` (button, input, textarea, native select, checkbox,
  switch, radio cards, field, badge, dialog, sheet, confirm host, dropdown,
  tooltip, segmented control, skeleton, empty/error state, progress, notice,
  toaster, copy button, key-value, kbd, stone stack, brand mark, contour field).
- Identity: warm stone neutrals, "juniper" teal brand accent, distinct
  ok/warn/bad/info tones, all defined once in `src/styles/app.css` and switched
  by the `.dark` class. Type: Bricolage Grotesque (titles), IBM Plex Sans (UI),
  IBM Plex Mono (identifiers, keys, JSON). Signature: the stone-stack glyph
  encodes the six pipeline stages of a document; the login page sits on a
  slowly drifting contour field (static under reduced motion).
- Information architecture: sidebar groups Work (overview, knowledge bases),
  Connect (API keys, MCP service), Setup (models, storage), Administration
  (users, audit, settings), filtered by capability. Ctrl+K command palette
  jumps to pages, knowledge bases and actions. User menu offers a voluntary
  password change (`POST /v1/auth/change-password`).
- Overview: setup checklist until model/storage/first base exist, a "needs
  attention" list built from failed documents, building indexes, rebuild
  required, MCP errors and stalled queues, the base list with processing/failed
  counts, and the real retrieval/MCP endpoints to copy.
- Knowledge base detail: facts strip, build progress from `index-progress`,
  segmented sections Documents / Search / Index versions / Settings kept in the
  URL. Documents is master-detail: state filters and drop-zone upload on the
  left, the selected document (facts, error with retry, chunks with in-place
  edit) on the right; `?doc=` and `?state=` deep-link. Search runs the public
  query with mode, fusion, weights, parent context and rerank options, shows
  degradation notices, latency and cited hits that link back to the document.
  Settings edit name, retrieval defaults (including weighted fusion and rerank)
  and chunking, and expose deletion. Restricted content shows the break-glass
  request instead of a bare 403.
- Remaining pages rebuilt on the same primitives: models (providers/models with
  inline live test results), storage (bindings with inline health), API keys
  (scopes with meaning, knowledge-base picker, expiry, connect panel with a real
  curl example), users (grants sheet with knowledge-base picker), audit
  (segmented quick filters, expandable JSON), settings (radio cards, hot/restart
  badges, queue health), MCP service (status facts, actions in the header,
  endpoints with copy, configuration, live logs).
- States: skeletons while loading, actionable empty states, error states with
  request IDs and retry, an offline banner driven by browser connectivity plus
  API transport failures, a toast when a route chunk fails to load, and toasts
  for every completed action.
- Strings: every user-visible string lives in `src/locales/{en-US,zh-CN}.json`;
  `npm run i18n:check` fails on unknown or unbalanced keys and runs in the build
  and the web image.
- Branding hook: `src/app/brand.ts` (name, logo, favicon) so the company fork
  replaces one file.

## Verification (2026-09-23)

- `npm run i18n:check` 580 keys; `vue-tsc --noEmit` clean; `vitest run`
  **60 tests in 11 files** (rewritten for the new components: forced credential
  dialog non-dismissable, secret reveal, permissions, API client/session, shell
  navigation/palette/theme/offline, knowledge list/detail/search/chunk edit,
  polling cadence, MCP view/API); `vite build` succeeds; production web image
  `deploy/docker/Dockerfile.web` builds (api drift + i18n + typecheck + build).
- Live browser acceptance `scripts/browser_ui_acceptance.py` against a real
  local stack (API + five workers on the disposable `pdf-20260921`
  PostgreSQL/pgvector, Redis and pinned TEI; Vite dev server proxy): sign-in,
  forced credential change, provider/model with healthy live test, object and
  vector bindings, knowledge base creation, two uploads indexed through the real
  pipeline, chunk edit, hybrid search with citations and parent expansion, hit to
  document navigation, retrieval defaults saved, overview attention/all-clear,
  API key creation and an agent-style bearer query through `/v1/retrieval/query`,
  user creation and a grant, audit/settings/MCP pages, command palette
  navigation, dark theme, zh-CN, offline banner and retry, mobile drawer and
  stacked master-detail, reduced motion, sign out and keyboard-only sign in.
  **24 checks pass, no console errors, no unexpected failed responses.**
  Screenshots under ignored `data/ui-20260922/shots`.
- Legacy smoke scripts (`browser_product_smoke.py`,
  `browser_mcp_management_smoke.py`) were ported to the new selectors; they were
  not re-executed against a Compose deployment in this round.

## Limits

- No new backend capability: reranking still needs an operator endpoint, and
  the search console disables the option when `/v1/meta` reports it absent.
- The overview fetches document counts for the first eight knowledge bases only.
- The browser acceptance uses Markdown/text uploads; OCR was disabled in the
  local stack because Tesseract is not installed on this workstation.
