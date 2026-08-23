import AppKit
import Foundation

/// A manual update check. Never automatic.
///
/// burn-o-meter promises no network, and an update check is a network call — one
/// that would report your IP, your version and your usage rhythm to a server on a
/// schedule you did not choose. That is telemetry however it is labelled, so this
/// runs only when someone picks it from the menu, and the menu item says so.
///
/// It also does not install anything. The app is unsigned, so replacing itself
/// would be both technically awkward and exactly the capability you least want an
/// unsigned binary to have. It reports what is available and how to get it.
enum Updates {

    static let releasesURL = URL(
        string: "https://github.com/devopsinside/burn-o-meter/releases/latest"
    )!

    private static let apiURL = URL(
        string: "https://api.github.com/repos/devopsinside/burn-o-meter/releases/latest"
    )!

    static func check(from button: NSStatusBarButton?) {
        var request = URLRequest(url: apiURL)
        request.timeoutInterval = 10
        // No token, no cookies, no identifiers — an unauthenticated read of a
        // public endpoint and nothing more.
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.httpShouldHandleCookies = false

        URLSession.shared.dataTask(with: request) { data, _, error in
            DispatchQueue.main.async {
                guard error == nil, let data,
                      let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let tag = root["tag_name"] as? String
                else {
                    present(title: "Could not check for updates",
                            body: "The check needs a network connection. Nothing else "
                                + "about burn-o-meter does.")
                    return
                }
                report(latest: tag.hasPrefix("v") ? String(tag.dropFirst()) : tag)
            }
        }.resume()
    }

    private static func report(latest: String) {
        let current = Preferences.version
        guard compare(latest, isNewerThan: current) else {
            present(title: "Up to date", body: "burn-o-meter \(current) is the latest release.")
            return
        }

        let alert = NSAlert()
        alert.messageText = "burn-o-meter \(latest) is available"
        alert.alertStyle = .informational
        if let cmd = upgradeCommand() {
            alert.informativeText = "You have \(current).\n\nRuns: \(cmd.joined(separator: " "))"
            alert.addButton(withTitle: "Upgrade Now")
            alert.addButton(withTitle: "Open Releases")
            alert.addButton(withTitle: "Later")
        } else {
            // No recognised install to upgrade — offering a button that cannot work
            // is worse than sending someone to the page that can.
            alert.informativeText =
                "You have \(current).\n\nThis install was not made by Homebrew, pipx or uv, "
                + "so there is no upgrade command to run for you."
            alert.addButton(withTitle: "Open Releases")
            alert.addButton(withTitle: "Later")
        }

        NSApp.activate(ignoringOtherApps: true)
        let choice = alert.runModal()
        if let cmd = upgradeCommand(), choice == .alertFirstButtonReturn {
            runUpgrade(cmd, to: latest)
        } else if choice == .alertSecondButtonReturn || (upgradeCommand() == nil && choice == .alertFirstButtonReturn) {
            NSWorkspace.shared.open(releasesURL)
        }
    }

    /// Runs the upgrade the user asked for, and reports what actually happened.
    ///
    /// Only the engine is upgraded. The `.app` is unsigned, so replacing itself is
    /// both awkward and the last capability an unsigned binary should have — the
    /// result says so rather than leaving someone to assume the whole thing moved.
    private static func runUpgrade(_ argv: [String], to latest: String) {
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: argv[0])
            process.arguments = Array(argv.dropFirst())
            let pipe = Pipe()
            process.standardOutput = pipe
            process.standardError = pipe

            var output = ""
            var ok = false
            do {
                try process.run()
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                output = String(data: data, encoding: .utf8) ?? ""
                ok = process.terminationStatus == 0
            } catch {
                output = error.localizedDescription
            }

            let tail = output
                .split(separator: "\n")
                .suffix(6)
                .joined(separator: "\n")

            DispatchQueue.main.async {
                if ok {
                    present(
                        title: "Upgraded to \(latest)",
                        body: "The command line tool is now \(latest).\n\n"
                            + "The menu bar app is separate and still \(Preferences.version) — "
                            + "rebuild it from a checkout with macos/make-app.sh."
                    )
                } else {
                    present(
                        title: "Upgrade did not complete",
                        body: (tail.isEmpty ? "The command failed." : tail)
                            + "\n\nRun it yourself: \(argv.joined(separator: " "))"
                    )
                }
            }
        }
    }

    /// How to upgrade depends on how it was installed, and the engine's recorded
    /// path is the evidence — telling a Homebrew user to run pipx would be worse
    /// than saying nothing.
    private static func upgradeCommand() -> [String]? {
        // Resolve symlinks first. pipx and uv both put a shim on PATH that points
        // into their own venv directory — `~/.local/bin/burnometer` says nothing
        // about who installed it, while its target says everything.
        let recorded = Engine.resolve()?.first ?? ""
        let path = URL(fileURLWithPath: recorded).resolvingSymlinksInPath().path
        func firstExisting(_ candidates: [String]) -> String? {
            candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
        }
        // Order matters: a pipx or uv install can also sit under /opt/homebrew if
        // that is where its Python came from, so the more specific markers win.
        if path.contains("/pipx/"), let pipx = firstExisting(
            ["/opt/homebrew/bin/pipx", "/usr/local/bin/pipx", NSHomeDirectory() + "/.local/bin/pipx"]
        ) {
            return [pipx, "upgrade", "burn-o-meter"]
        }
        if path.contains("/uv/"), let uv = firstExisting(
            ["/opt/homebrew/bin/uv", "/usr/local/bin/uv", NSHomeDirectory() + "/.local/bin/uv"]
        ) {
            return [uv, "tool", "upgrade", "burn-o-meter"]
        }
        if path.contains("/Cellar/") || path.hasPrefix("/opt/homebrew") || path.hasPrefix("/usr/local"),
           let brew = firstExisting(["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]) {
            return [brew, "upgrade", "burn-o-meter"]
        }
        return nil
    }

    /// Compares dotted numeric versions. Anything unparseable sorts as older, so a
    /// malformed tag never nags someone who is already current.
    private static func compare(_ latest: String, isNewerThan current: String) -> Bool {
        func parts(_ s: String) -> [Int] {
            s.split(separator: ".").map { Int($0.prefix(while: \.isNumber)) ?? 0 }
        }
        let a = parts(latest), b = parts(current)
        for i in 0..<max(a.count, b.count) {
            let l = i < a.count ? a[i] : 0
            let r = i < b.count ? b[i] : 0
            if l != r { return l > r }
        }
        return false
    }

    private static func present(title: String, body: String, openReleases: Bool = false) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = body
        alert.alertStyle = .informational
        if openReleases {
            alert.addButton(withTitle: "Open Releases")
            alert.addButton(withTitle: "Later")
        } else {
            alert.addButton(withTitle: "OK")
        }
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn && openReleases {
            NSWorkspace.shared.open(releasesURL)
        }
    }
}
