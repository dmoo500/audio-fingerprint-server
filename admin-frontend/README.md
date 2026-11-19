# Fingerprint admin frontend

The admin UI is a Vue 3 and TypeScript application. It uses the same package
versions as `recognition-frontend` and talks to the same-origin JSON API under
`/admin/api` and the live event stream at `/admin/events`.

Install dependencies with `pnpm install`, run component tests with `pnpm test`,
and build with `pnpm build`. The production build writes to
`../fingerprint_server/admin/dist` and uses `/admin/` as its base path.
For an isolated build that does not write into the backend tree, run
`pnpm exec vue-tsc --noEmit && pnpm exec vite build --outDir ./build`.
