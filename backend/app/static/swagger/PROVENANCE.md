# Vendored Swagger UI assets

These files let `/docs` work with no internet access. They are served by
`app/api/docs.py` from an explicit whitelist (not a directory mount).

- **Package:** `swagger-ui-dist` **5.33.1** (npm registry), from `npm pack swagger-ui-dist@5`
- **Upstream:** https://github.com/swagger-api/swagger-ui
- **License:** Apache License 2.0 (`LICENSE`); copyright notice in `NOTICE`.
  Third-party licenses bundled inside the JavaScript are listed in
  `swagger-ui-bundle.js.LICENSE.txt`. Redistribution is permitted provided these
  license files stay with the code.
- **Retrieved:** 2026-10-09
- **Modified:** no. Files are byte-for-byte copies of the package contents.

| File | SHA-256 |
|---|---|
| `swagger-ui-bundle.js` | `050bc415ee7048dcd881682678f720264e7da5e373f7461d7c58c755305255f7` |
| `swagger-ui.css` | `1ac324f7dcd27e4b9386b4bd6421271ec147e922a22c05ba24b11515e9aa6321` |
| `favicon-32x32.png` | `3ed612f41e050ca5e7000cad6f1cbe7e7da39f65fca99c02e99e6591056e5837` |
| `LICENSE` | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` |
| `NOTICE` | `0d20d1adef18aee3f40dd258172155521ce702ac445cb5f7b7d60ed32dad2fb2` |
| `swagger-ui-bundle.js.LICENSE.txt` | `63818894e4b04cd0e3180d9cb20761e227a939121e7484f8e1d528227c756f89` |

To update: `npm pack swagger-ui-dist@5`, copy the same files, and refresh this table.
ReDoc is intentionally not vendored; `/redoc` is disabled.
