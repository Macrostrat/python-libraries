# Macrostrat authentication system

This module contains tools to manipulate Macrostrat's user authentication
system. It is divided into two submodules:

- `macrostrat.auth_system.legacy`: A JWT-based authentication system relying on
  local storage of hashed passwords. This system was created as part
  of [Sparrow](https://sparrow-data.org) and is being phased out in favor of a
  more modern system based on ORCID.
- `macrostrat.auth_system.core`: An ORCID-based user
  authentication system. This system will become the primary authentication
  system for Macrostrat, but it is still in development.

- `macrostrat.auth_system.session`: ORCID sign-in for any FastAPI service,
  extracted from Macrostrat's API v3. `create_auth_router(settings, store)`
  provides the login, callback, refresh, logout and `me` routes; the application
  supplies a `UserStore` that maps an ORCID iD to its own account and Postgres
  role. `SessionReader` is the matching request dependency. The access token is
  also a valid PostgREST JWT. First used by Mapboard.

We plan to gradually converge the functionality of both versions while phasing
out the legacy system.

The system has tests that can be run with `poetry run pytest auth-system`
(currently, only the legacy system is covered).

## Key planned functionality

- Allow many Macrostrat-hosted services to easily integrate with Macrostrat's
  login and token flow
- Allow APIs to easily validate user credentials and tokens with minimum
  overhead
- Allow access to be checked in multiple ways:
  - Cookies and headers
  - Limited-time JWT tokens and long-duration, cancelable API tokens
  - Verify against Macrostrat "user group" or application-specific criteria (
    e.g., a list of authorized ORCID IDs)

