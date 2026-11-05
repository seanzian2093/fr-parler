# Porting fr-parler to iOS

Status: Phase 1 done (bridge-first shim in `pronounce.py`, 2026-11-04); Phases
2–6 not started. Created 2026-11-04.

## The core insight

fr-parler is a static site: Markdown notes build into self-contained HTML with
click-to-pronounce 🔊 buttons (browser Web Speech API), packaged as a PWA and
deployed to GitHub Pages. **The only thing that breaks on iOS is TTS** — WebKit
silently no-ops `speechSynthesis`, so the 🔊 does nothing on-device. Every iOS
"browser" (Safari, Chrome, the PWA) is WebKit, so switching browsers doesn't help.

You don't re-implement the app. The content pipeline (Markdown → HTML, the
sidebar, the icons, offline) all work as-is inside a `WKWebView`. The port is **one
thin native wrapper** that (1) loads the same `build/*.html` and (2) replaces the
broken Web-Speech call with a native `AVSpeechSynthesizer` call. One HTML build,
two front-ends: the PWA keeps serving desktop/Android, the native app serves iOS.
The `pronounce.py` builder changes by **one backward-compatible shim** in the
emitted JS.

A full SwiftUI rewrite is *not* worth it — it would split the content into a second
source and throw away the pipeline. Skip it.

```
notes/*.md ──▶ pronounce.py ──▶ build/*.html ──┬─▶ GitHub Pages (PWA, desktop/Android)  [unchanged]
                                                └─▶ iOS app bundle ──▶ WKWebView ──▶ AVSpeechSynthesizer
                                                             ▲
            emitted 🔊 handler tries bridge first, falls back to Web Speech
```

## Phase 1 — Make the 🔊 handler bridge-first (1 small change, backward-compatible) — DONE 2026-11-04

In `scripts/pronounce.py`, `JS_CLICK_HANDLER` (the click path), change it so it
prefers a native bridge if present, else does exactly what it does today:

```js
var text = button.getAttribute("data-say");
if (!text) return;
// Native bridge (iOS app) — injected as window.fpbridge; undefined in PWA/desktop.
if (window.fpbridge && window.fpbridge.speak) { window.fpbridge.speak(text); return; }
// else: existing Web Speech API path, unchanged below
if (!("speechSynthesis" in window)) { alert(...); return; }
...
```

Because `fpbridge` is only defined inside the native app, **PWA/desktop behavior is
byte-for-byte identical** — no regression. This is the one and only change to the
builder.

## Phase 2 — Native iOS shell (Xcode, ~half a day)

The native side of the Phase 1 contract: a `WKWebView` that loads the bundled HTML
and answers the `fpbridge.speak()` call with `AVSpeechSynthesizer`. Two parts only
you can do (Xcode's project creation and signing are GUI); the code is ready to
paste. Smallest path to a working 🔊 on-device:

### Step 1 — Create the project (GUI, ~2 min)

Xcode → **File → New → Project → iOS → App**. Name it `fr-parler` (or `FrParler`),
language **Swift**, Interface **Storyboard**, Team = your free Apple ID. Xcode
generates a `ViewController.swift` — you replace its contents.

### Step 2 — The bridge (the heart of it)

Replace the generated `ViewController.swift`:

```swift
import UIKit
import WebKit
import AVFoundation  // AVSpeechSynthesizer, AVSpeechUtterance, AVSpeechSynthesisVoice

final class ViewController: UIViewController {
    private var webView: WKWebView!
    private let synth = AVSpeechSynthesizer()
    // nil if the French voice isn't downloaded yet (see step 4)
    private let ttsVoice = AVSpeechSynthesisVoice(language: "fr-FR")

    override func viewDidLoad() {
        super.viewDidLoad()
        // Match the page's dark bg so there's no flash before the web view paints.
        view.backgroundColor = UIColor(red: 0x0f/255, green: 0x11/255, blue: 0x15/255, alpha: 1)

        // 1. Wire the JS <-> native bridge.
        //    Inject window.fpbridge at document start; a tap on a 🔊 button posts the
        //    French text to the "fp" handler below. It re-injects on every in-app
        //    navigation, so sibling pages (Vocabulary.html) get it too.
        let ucc = WKUserContentController()
        ucc.addUserScript(
            WKUserScript(
                source: "window.fpbridge = { speak: function(t){ window.webkit.messageHandlers.fp.postMessage(t); } };",
                injectionTime: .atDocumentStart, forMainFrameOnly: true
            ), for: WKContentWorld.all  // qualify the type; bare .all fails to infer
        )
        ucc.add(self, name: "fp")

        let config = WKWebViewConfiguration()
        config.userContentController = ucc

        // 2. Pin the web view to the safe area (notch / home indicator).
        webView = WKWebView(frame: .zero, configuration: config)
        webView.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            webView.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor),
            webView.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor),
        ])

        // 3. Load the bundled HTML. index.html follows whatever --main was set to;
        //    allow read access to the whole folder so sibling-page links resolve in-app.
        guard let resources = Bundle.main.resourceURL else {
            showMissingContent(); return
        }
        let webFolders = [
            resources.appendingPathComponent("web", isDirectory: true),
            resources.appendingPathComponent("fr-parler", isDirectory: true)
                .appendingPathComponent("web", isDirectory: true),
        ]
        guard let web = webFolders.first(where: {
            FileManager.default.fileExists(atPath: $0.appendingPathComponent("index.html").path)
        }) else {
            showMissingContent()
            return
        }
        let index = web.appendingPathComponent("index.html")
        webView.loadFileURL(index, allowingReadAccessTo: web)
    }

    private func showMissingContent() {
        let l = UILabel()
        l.text = "The app bundle is missing web/index.html. Expected it at web/index.html or fr-parler/web/index.html."
        l.numberOfLines = 0; l.textAlignment = .center
        l.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(l)
        NSLayoutConstraint.activate([l.centerXAnchor.constraint(equalTo: view.centerXAnchor),
                                     l.centerYAnchor.constraint(equalTo: view.centerYAnchor),
                                     l.widthAnchor.constraint(equalToConstant: 260)])
    }
}

extension ViewController: WKScriptMessageHandler {
    func userContentController(_ uc: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "fp", let text = message.body as? String else { return }
        let u = AVSpeechUtterance(string: text)
        u.voice = ttsVoice               // nil when fr isn't installed -> system default voice
        u.rate = 0.45                    // Web Speech's 0.9 doesn't map 1:1; AVSpeech default ~0.5 — tune on-device
        synth.stopSpeaking(at: .immediate)
        synth.speak(u)
    }
}
```

Two gotchas (already baked into the code):
1. AVSpeech's `rate` scale differs from Web Speech's — 0.9 does *not* transfer;
   start at ~0.45 and tune on-device.
2. `AVSpeechSynthesisVoice(language:"fr-FR")` returns `nil` until the user
   downloads a French voice; the code falls back to the system voice, and a
   first-run prompt to download it is Phase 5 polish.

### Step 3 — Bundle the HTML (the one fiddly bit)

Generate it PWA-free into a folder inside the project (run from repo root):

```
python3 scripts/pronounce.py --all --main Complete-French-All-in-One --no-pwa --out ios/fr-parler/web
```

The project includes `ios/fr-parler/web` as a folder resource, so the complete
directory (including sibling pages) is copied into the app bundle. Regenerate to
that path whenever the notes change.

### Step 4 — Install the French voice, then run

The #1 gotcha: TTS is silent until a voice exists. **Settings → Accessibility →
Spoken Content → Voices → Manage Voices → download "French (France)."** Then set the
run destination to your physical iPhone, build & run, and tap a 🔊.

**Milestone that means Phase 2 has started:** one word spoken by native
`AVSpeechSynthesizer`, in a real app on a real device. Everything else — nav bar,
haptics, automating the build, distribution — is after that.

## Phase 3 — Bundle the built HTML (offline)

A "Run Script" build phase runs the stdlib Python builder and embeds the output as a
resource — no network needed at runtime, matching the offline ethos:

```
python3 scripts/pronounce.py --all --main Complete-French-All-in-One --no-pwa --out <embedded-resource-dir>
```

For the Run Script build phase (Run Before *Compile Sources*), with the Xcode
project at `ios/fr-parler.xcodeproj`, point the script and output at the
project-relative paths:

```
python3 "$PROJECT_DIR/../scripts/pronounce.py" --all --main Complete-French-All-in-One --no-pwa --out "$PROJECT_DIR/fr-parler/web"
```

Use `--no-pwa`: the webview bundles content locally so `sw.js` /
`manifest.webmanifest` are dead weight (WKWebView has no service-worker support —
the existing `register(...).catch()` already no-ops, but `--no-pwa` keeps the
bundle clean).

## Phase 4 — Test on your device

1. Install a French voice: **Settings → Accessibility → Spoken Content → Voices →
   Manage → download "French (France)"** — *the prerequisite most people miss.*
2. Run to the physical iPhone (free Apple ID → signed 7-day build).
3. Tap a 🔊 → confirm it speaks French; tap a sidebar link → confirm in-app
   navigation; check light/dark.

## Phase 5 — Polish (optional, later)

Native `UITabBar` / split-view instead of the HTML sidebar; haptic tick on tap; a
first-run banner that routes to voice-download Settings; webview↔native appearance
sync. All non-blocking.

## Phase 6 — Distribution (only if you want to share)

Free account = 7-day device builds, no distribution. To share: $99/yr account →
TestFlight → App Store. Not needed to use it.

## What stays untouched

The Markdown notes, the build pipeline, CI / GitHub Pages, and the PWA for
desktop/Android all keep working. Net change: **one shim in `pronounce.py` + a new
`ios/` Xcode project.**

## Decision points (recommended in bold)

1. **Content delivery — bundle offline** (Phase 3) vs. load the live GitHub Pages
   URL. Bundle wins on the offline ethos.
2. **iOS build — `--no-pwa`** (Phase 3, recommended) vs. keep PWA files. They're
   inert in the webview.
3. **Navigation — keep the embedded HTML sidebar** for the MVP; native nav only in
   Phase 5.
