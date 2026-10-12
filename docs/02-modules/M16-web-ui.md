# M16 — Web User Interface

| | |
| --- | --- |
| **Package** | `apps/web` |
| **Layer** | frontend — HTTP client only |
| **Phase** | 1 → 5 (incremental, tracks backend phases) |
| **Owner** | Frontend eng. E (+ F from Phase 2) |
| **Depends on** | the generated OpenAPI client only |
| **Requirements owned** | FR-P-01..10, FR-A-04..07 (UI half), FR-L-10, FR-M-07 |

---

## 1. Purpose and scope

The human surface. Machines use the API; people use this.

**In scope:** authentication including the forced credential dialog, admin console, KB
management, document and chunk browsing, the retrieval test console, pipeline editor, function
editor, evaluation dashboards, i18n.

**Out of scope:** an end-user chat product (ADR-0008). The retrieval console is a *testing* tool.

---

## 2. Structure

```
apps/web/src/
├── api/                     GENERATED from /v1/openapi.json — never hand-edited
├── shared/
│   ├── tokens/              design tokens: colour, spacing, type scale
│   ├── components/          DataTable, StatusBadge, JsonViewer, EmptyState, ErrorBoundary
│   ├── composables/         usePolling, usePermissions, useProblemDetail
│   └── i18n/                en-US.json, zh-CN.json
├── features/
│   ├── auth/                login, ForcedCredentialDialog
│   ├── users/ grants/ api-keys/
│   ├── knowledge/           list, create wizard, settings
│   ├── documents/           table with live status, upload, detail
│   ├── chunks/              browser, editor, split/merge
│   ├── retrieval-console/   query form, results, explain viewer
│   ├── crawl/ models/ pipelines/ functions/ evaluation/
│   └── admin/               settings, audit log, usage
└── app/                     router + guards, providers, layout
```

Features never import each other; shared code moves to `shared/`. `api/` is regenerated in CI —
a diff fails the build if it was hand-edited.

---

## 3. The screens that matter most

### 3.1 Forced credential dialog (`FR-P-02`, `FR-A-04..07`)

The first thing every user of every deployment sees. It must be impossible to circumvent in the
UI *and* obviously not the security boundary — the server already refuses everything else.

```
┌─────────────────────────────────────────────────────────┐
│  Set your credentials                                    │   no ✕, no ESC, no backdrop close
│                                                          │
│  This account is using a generated initial password.     │
│  Choose new credentials to continue.                     │
│                                                          │
│  Username        [ admin                    ]  optional  │   ← FR-A-07
│  Current password[ ••••••••••••             ]            │
│  New password    [ ••••••••••••             ]            │
│  Confirm         [ ••••••••••••             ]            │
│                                                          │
│  ✓ At least 12 characters                                │   live, from server policy
│  ✓ Upper and lower case                                  │
│  ✗ At least one digit or symbol                          │
│                                                          │
│                              [ Save and continue ]       │
└─────────────────────────────────────────────────────────┘
```

Implementation requirements:
- Router **navigation guard** blocks every route while `must_change_password` is true.
- A global response interceptor: any `PASSWORD_CHANGE_REQUIRED` reopens the dialog, which
  satisfies "reappears on next login" for free — the state is server-side, so a browser close,
  a new tab, or a container restart all land back here.
- Policy rules render from the server's `policy` object; the client never hardcodes them.
- Username field is prefilled and optional; submitting both is one request.
- On success, the app boots normally — no reload, no second login.

### 3.2 Document list with live status (`FR-P-05`)

The most-complained-about screen in every RAG product. Get it right.

```
┌───────────────────────────────────────────────────────────────────────────────┐
│ Documents · Ops Handbook                    [ Upload ]  [ Add crawl ]  [ ⟳ ]  │
├──────────────────────┬───────────┬──────────────┬────────┬────────────────────┤
│ Name                 │ State     │ Stage        │ Chunks │ Message            │
├──────────────────────┼───────────┼──────────────┼────────┼────────────────────┤
│ handbook-2026.pdf    │ ● Indexed │ —            │    312 │                    │
│ policy-v3.docx       │ ◐ Working │ Embedding 62%│      — │                    │
│ scan-archive.pdf     │ ◐ Working │ OCR page 8/45│      — │                    │
│ contract-final.pdf   │ ✕ Failed  │ Parse        │      — │ Password-protected.│
│                      │           │              │        │ Remove protection  │
│                      │           │              │        │ and re-upload. [↻] │
│ duplicate-copy.pdf   │ ○ Skipped │ —            │      — │ Same content as    │
│                      │           │              │        │ handbook-2026.pdf  │
└──────────────────────┴───────────┴──────────────┴────────┴────────────────────┘
```

- Poll every 3 s while any document is in a working state; stop when all are terminal.
- Error messages come from M07's contributor-facing table — never a raw code, never a stack.
- Every failure has a retry affordance.
- Skipped duplicates link to the original.

### 3.3 Retrieval test console (`FR-P-06`)

The screen a knowledge engineer lives in.

```
┌──────────────── Query ─────────────────┐┌─────────── Results ────────────────┐
│ How do I rotate the signing key?       ││ 1. Security > Key Management  p.14  │
│                                        ││    0.873  ▸ dense 0.81 sparse 12.4 │
│ Targets  [Ops Handbook ×] [+]          ││    Key rotation is performed via…   │
│ Mode     ( ) vector (•) hybrid ( ) fts ││                                     │
│ top_k    [5 ]   candidates [100]       ││ 2. …                                │
│ Rerank   [✓] bge-reranker-v2-m3        ││                                     │
│ Threshold[0.35]  Budget [4000] tokens  │└─────────────────────────────────────┘
│ Filters  { "meta.lang": "en" }         │┌────────── Explain ─────────────────┐
│ Explain  [✓]                           ││ embed   9ms  cache HIT              │
│                     [ Run ]  [ Copy ⧉ ]││ dense  21ms  100 hits  top=chk_9f…  │
└────────────────────────────────────────┘│ sparse 18ms  100 hits  top=chk_2a…  │
                                          │ fuse    1ms  rrf  143 unique        │
  [ Copy ⧉ ] yields a ready-to-run curl   │   chk_2a: dense#7 sparse#1 → #2      │
  with the exact payload — this is how    │ rerank 47ms  chk_2a #2 → #1 (0.873) │
  an engineer moves from console to app.  │ expand  3ms  5 parents               │
                                          └─────────────────────────────────────┘
```

The explain panel is the reason engineers will keep this tab open. "Copy as curl" is the bridge
from exploration to integration and should produce a payload that runs verbatim.

### 3.4 KB creation wizard (`FR-P-04`)

Four steps: Basics (name, description, icon) → Embedding model (**with an immutability warning
stated plainly**) → Storage (vector + object binding) → Chunking & retrieval (with a preview on
a sample document). The immutability warning must be prominent — this is the one irreversible
choice in the flow, and burying it produces support tickets.

### 3.5 Pipeline editor (`FR-P-07`, `FR-L-10`)

Vue Flow canvas: node palette by category, drag to place, connect ports (type-incompatible ports
refuse to connect and say why), inline node configuration, validate before publish with errors
anchored to the offending node, test-run with per-node input/output inspection.

---

## 4. Cross-cutting UI conventions

| Concern | Rule |
| --- | --- |
| Errors | Read `code` from the problem+json, map to a translated message, fall back to `detail`. Always show `request_id` for support. |
| Permissions | `usePermissions()` hides unavailable actions. The server is still authoritative — the UI is convenience, not enforcement. |
| Loading | Skeletons, not spinners, for content areas |
| Empty states | Every list explains what it is and how to add the first item |
| Destructive actions | Confirmation naming the resource; KB deletion requires typing the name |
| Long operations | Progress with an ETA where derivable; never an indeterminate bar for something measurable |
| Polling | 3 s while active; back off to 10 s after 2 minutes; stop when terminal |
| Time | Relative ("3 minutes ago") with an absolute UTC tooltip |
| Numbers | Thousands separators; bytes humanized; latency in ms |
| i18n | No hardcoded strings; `zh-CN` complete before Phase 3 exit |
| Accessibility | Keyboard navigable, focus visible, labelled inputs, AA contrast (`FR-P-10`) |

---

## 5. Test requirements

| ID | Test | Req |
| --- | --- | --- |
| TC-M16-01 | **Forced dialog cannot be dismissed by ✕, ESC, or backdrop** | FR-P-02 |
| TC-M16-02 | **Router guard blocks every route while the flag is set** | FR-P-02 |
| TC-M16-03 | **`PASSWORD_CHANGE_REQUIRED` from any call reopens the dialog** | FR-P-02 |
| TC-M16-04 | Policy rules render from the server response, not hardcoded | FR-A-09 |
| TC-M16-05 | Username + password submit as one request | FR-A-07 |
| TC-M16-06 | Document list polls and stops when terminal | FR-P-05 |
| TC-M16-07 | Failed documents show the friendly message and a retry | FR-P-05 |
| TC-M16-08 | Retrieval console sends every parameter correctly | FR-P-06 |
| TC-M16-09 | Explain panel renders all stages and rank changes | FR-P-06 |
| TC-M16-10 | "Copy as curl" produces a runnable request | §3.3 |
| TC-M16-11 | KB wizard shows the immutability warning before commit | FR-P-04 |
| TC-M16-12 | Pipeline editor refuses type-incompatible connections | FR-L-10 |
| TC-M16-13 | Actions the user lacks permission for are hidden | FR-P-03 |
| TC-M16-14 | All strings resolve in both locales (no missing keys) | FR-P-08 |
| TC-M16-15 | Primary flows are keyboard-navigable at AA contrast | FR-P-10 |
| TC-M16-16 | Generated API client matches the committed OpenAPI | §2 |

E2E (Playwright): J1 first deployment, J2 onboarding, J3 KB build, J7 evaluation.

---

## 6. Acceptance criteria

- [ ] E2E journeys pass in CI on Chrome and Firefox
- [ ] No hand-edits in `api/` (CI diff check)
- [ ] Lighthouse performance ≥ 85 on the KB list and document list
- [ ] Both locales complete with no missing keys
- [ ] Bundle < 1.5 MB gzipped for the initial route

---

## 7. Task breakdown

| Task | Description | Est (d) | Phase |
| --- | --- | --- | --- |
| T-M16-01 | Scaffold: Vite, TS, Element Plus, Pinia, router, i18n | 1.5 | 0 |
| T-M16-02 | OpenAPI client generation + CI check | 0.5 | 0 |
| T-M16-03 | Design tokens + shared components | 2.0 | 1 |
| T-M16-04 | **Login + forced credential dialog + guards** | 2.0 | 0 |
| T-M16-05 | App shell, navigation, permission composable | 1.5 | 1 |
| T-M16-06 | User management screens | 2.0 | 1 |
| T-M16-07 | Grants and permission editor | 2.0 | 1 |
| T-M16-08 | API key management | 1.5 | 1 |
| T-M16-09 | Audit log viewer | 1.5 | 1 |
| T-M16-10 | KB list + creation wizard | 2.5 | 2 |
| T-M16-11 | KB settings | 2.0 | 2 |
| T-M16-12 | **Document list with live status + upload** | 3.0 | 2 |
| T-M16-13 | Chunk browser + editor | 2.5 | 2 |
| T-M16-14 | **Retrieval console + explain viewer** | 3.0 | 2 |
| T-M16-15 | Crawl job management | 2.0 | 3 |
| T-M16-16 | Model provider and model screens + chat console | 2.5 | 3 |
| T-M16-17 | i18n zh-CN completion | 1.5 | 3 |
| T-M16-18 | **Pipeline editor (Vue Flow)** | 5.0 | 4 |
| T-M16-19 | Function editor (Monaco) + test panel | 3.0 | 4 |
| T-M16-20 | Evaluation dashboards + comparison charts | 3.0 | 5 |
| T-M16-21 | Usage and quota screens | 1.5 | 5 |
| T-M16-22 | Accessibility pass | 1.5 | 5 |
| T-M16-23 | E2E tests | 3.0 | 2–5 |
| | **Total** | **51.0** | |
