# Static website deployment

The [TextFlowKit landing page](https://www.textflowkit.org/) is the static
`docs/index.html` file. It does **not** run the CLI, MCP server, HTTP adapter,
or speech models. A public transcription service is a separate, future
deployment with its own security and capacity design.
The landing page links to the versioned [user manual](user-manual.md), which
is rendered on GitHub rather than by the static Pages site.

## Cloudflare Pages configuration

- Plan: Cloudflare Pages Free.
- Project: `textflowkit`.
- Source: GitHub repository `scottconverse/textflowkit`, production branch
  `main`. The Cloudflare Workers and Pages GitHub App is scoped to this
  repository only.
- Root directory: `docs`.
- Build command: `exit 0` (the page is already static HTML).
- Build output directory: `.` (relative to `docs`).
- Environment variable: `SKIP_DEPENDENCY_INSTALL=1`. This prevents Pages
  from trying to install the repository's Python transcription dependencies.
- Pages preview address: <https://textflowkit.pages.dev/>.
- Custom domains: `www.textflowkit.org` and `textflowkit.org`. Cloudflare DNS
  has proxied CNAMEs for both to `textflowkit.pages.dev`.
- Canonical URL: `https://www.textflowkit.org/`. A Cloudflare Single Redirect
  named `Canonical apex to www` sends HTTPS apex requests there with status
  301, preserving path and query string. Cloudflare's HTTP-to-HTTPS redirect
  handles HTTP requests before this rule.

Merging a change to `main` under `docs/` triggers a new Pages deployment.
Check the Pages deployment status and verify the public site over HTTPS after
each website change. GitHub CI remains the deterministic code/test gate;
Cloudflare Pages is only the static website host.

## Unknown paths and the 404 page

Pages returns a top-level `404.html` for a missing file. Without that file it
assumes single-page-application routing and matches every unmatched path to
`/`, so an unknown URL would serve the landing page with HTTP 200. The
`docs/404.html` file is the response for unknown paths. It is a static page,
like the landing page: it runs no scripts and makes no request to the
transcription pipeline.

**Pending until the next deployment.** The deployed site still returns 200 for
unknown paths until `docs/404.html` reaches production. After the change is
merged and deployed, request a random unknown path (for example
`https://www.textflowkit.org/__missing_route__`) and confirm both that the
status is **404** and that the returned content is a not-found page distinct
from the home page. Record the observed status and the page title. A local file
server is not Cloudflare Pages, so this check can only run after deployment.

The former GitHub Pages `docs/CNAME` file is intentionally absent. Do not
recreate it: domain routing now lives in Cloudflare, not GitHub Pages.
