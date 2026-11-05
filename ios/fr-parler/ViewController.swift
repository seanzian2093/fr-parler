//
//  ViewController.swift
//  fr-parler
//
//  Created by SeanZ on 2026-11-05.
//
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
            )
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
