# Local browser dependency

Chart.js 4.4.8 is vendored unchanged from:
- https://cdn.jsdelivr.net/npm/chart.js@4.4.8/dist/chart.umd.js
- https://cdn.jsdelivr.net/npm/chart.js@4.4.8/LICENSE.md

Copyright Chart.js Contributors. MIT; see `Chart.js-LICENSE.md`.

SHA-256:
- chart.umd-4.4.8.js: e4cf4d144b222634f2e64ff707cd57b953f8de4b65b231cd5ac6bde114648e4d
- Chart.js-LICENSE.md: 41a84aa2caba645f966a18d9c2056b73e6d3a81d80bc0046bc0011a2634d4cce

Runtime report requests use local `/static/vendor/chart.umd-4.4.8.js`; no CDN connection is needed. The upstream source-map comment is only a developer-tools reference; no source map is shipped or required at runtime.
