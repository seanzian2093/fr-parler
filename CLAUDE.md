# fr-parler

A repo for self-teaching French: Markdown notes that build into self-contained,
click-to-pronounce HTML pages, deployed to GitHub Pages.

## Verification & testing — you run the commands

When verifying or testing, **do not run scripts or Bash commands yourself.** Print
the exact command(s) and what to check, and let the user run them locally — it's
faster and sidesteps tooling limits. Only execute a command the user explicitly
asks you to run. Example to hand off rather than run:

```
python scripts/pronounce.py --all --main Complete-French-All-in-One
```

## Build & deploy

- Build every `notes/*.md` → `build/*.html` plus a redirecting `index.html`:
  ```
  python scripts/pronounce.py --all
  ```
- Choose the landing page (what `index.html` redirects to) with `--main <stem>`:
  ```
  python scripts/pronounce.py --all --main Complete-French-All-in-One
  ```
- Build one page explicitly: `python scripts/pronounce.py notes/Foo.md build/Foo.html`
- **CI** (`.github/workflows/pages.yml`) runs the `--all --main Complete-French-All-in-One`
  build on every push to `main` and deploys `build/` to GitHub Pages. No third-party
  deps — plain `python` (repo pins 3.13 via `.python-version`); the only JS in the output
  is the fixed, emitted click handler, the service-worker registration, and the
  landing redirect.

## PWA: installable + offline (mobile)

By default the build makes the site a **progressive web app** so it can be installed
to a phone's home screen (Android Chrome, or iOS "Share → Add to Home Screen") and
opened offline.  For that, `pronounce.py` emits three more files into `build/`:

- `manifest.webmanifest` — app name, icon, and start page (the `--main` page).
- `sw.js` — a service worker that precaches every page (cache-first) and falls back
  to the main page when offline.
- `icon-192.png`, `icon-512.png`, `icon-maskable-512.png` — the home-screen icons,
  drawn from a rounded indigo gradient + a white speaker glyph by the stdlib alone
  (`zlib`/`struct`) — no image library, no binary asset in the repo.

Each page also links the manifest and registers `sw.js`. All URLs are relative, so
the same `build/` works at a GitHub Pages domain root or under a `/repo/` sub-path.
A lone `.html` still opens on its own — the PWA files degrade gracefully if absent.

Disable the PWA wiring with `--no-pwa` (plain HTML only). Note: the 🔊 pronunciation
still uses each device's own French TTS voice — it is **not** bundled audio.

## Notes authoring: the pronounce marker

French words are wrapped in `{{...}}` and the builder turns each into a click-to-hear
button (browser French TTS). The English gloss is optional and goes **inside** the braces:

- `{{word, gloss}}` → button shows `🔊 word` with the gloss in muted italics.
- `{{word}}` → button, no gloss.
- A backtick-wrapped marker `` `{{word}}` `` still becomes a button (backticks are stripped).

**Use the `{{word, gloss}}` form for every word that has an English cognate** — that is the
one canonical way to attach a gloss (both `Vocabulary.md` and `Complete-French-All-in-One.md`
follow it). Do **not** write `{{word}} -> english`; that leaves the English as prose after the
button instead of inside it.

Other build behavior worth knowing:

- `README.md` (and any stem in `SKIP_STEMS`) is not built as a page.
- With 2+ pages, every page gets a left sidebar linking to the others, current one highlighted;
  a lone page renders centered without a sidebar.
- A page's `<title>` comes from its first `# H1`, reducing any `{{word, gloss}}` to just the word.
