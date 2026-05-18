# Autofill matcher tests

Synthetic ATS-form fixtures the matcher is run against. Modeled on the markup
patterns Greenhouse, Lever, Workable, and Ashby actually emit.

## Run

```
cd chrome-extension/tests
npm install jsdom
node autofill.test.mjs
```

Expected: 11/11, 7/11 (Lever — 4 legitimate skips), 7/7, 4/4.

The fixtures **do not** cover Workday's custom combobox / shadow-DOM widgets —
those need a real browser. Add new fixtures here when a real-world page misses
a field the matcher should have caught.
