---
macrostrat.auth_system: minor
---

- Add `macrostrat.auth_system.session`: ORCID sign-in routes, JWT session cookies and a FastAPI session dependency over an application-supplied `UserStore`.
- Session tokens can name an audience (`AuthSettings.audience`, `JWT_AUDIENCE`); tokens for another audience are refused.
