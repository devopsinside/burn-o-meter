import Foundation

/// Invokes `burn-o-meter scan` so the popover shows current data.
///
/// The app still never writes to the database. It asks the sanctioned writer to
/// refresh and then re-reads the result, which keeps the read-only guarantee
/// exactly where it matters — a bug in this app cannot corrupt the store —
/// while removing the reason a meter would ever show stale numbers.
///
/// The executable path comes from `~/.burn-o-meter/engine.json`, written by the
/// engine itself. A GUI app launched from Finder inherits a minimal PATH and
/// would otherwise never find a venv, pipx or Homebrew install.
enum Engine {

    static func pointerPath() -> String {
        if let override = ProcessInfo.processInfo.environment["BURNOMETER_HOME"] {
            return (override as NSString).appendingPathComponent("engine.json")
        }
        return NSHomeDirectory() + "/.burn-o-meter/engine.json"
    }

    /// Places an engine ends up, for when the recorded one has moved.
    ///
    /// A GUI app launched from Finder inherits a minimal PATH, so these are spelled
    /// out rather than searched for. Homebrew's `opt` path is listed ahead of its
    /// `Cellar` path deliberately: `opt` is a stable symlink, while the Cellar path
    /// contains the version number and therefore changes on every upgrade.
    private static var fallbackPaths: [String] {
        let home = NSHomeDirectory()
        var paths = [
            "/opt/homebrew/bin/burnometer",
            "/opt/homebrew/opt/burn-o-meter/bin/burnometer",
            "/usr/local/bin/burnometer",
            "\(home)/.local/bin/burnometer",
            "/opt/homebrew/bin/burn-o-meter",
            "/usr/local/bin/burn-o-meter",
            "\(home)/.local/bin/burn-o-meter",
        ]
        // Whatever PATH we do have, in case someone installed somewhere unusual.
        if let env = ProcessInfo.processInfo.environment["PATH"] {
            for dir in env.split(separator: ":") {
                paths.append("\(dir)/burnometer")
            }
        }
        return paths
    }

    /// The engine's location, preferring what it recorded and falling back to a
    /// search when that has gone stale.
    ///
    /// The recorded path goes stale routinely: `brew upgrade` moves the binary into
    /// a new versioned directory, and reinstalling through a different tool moves it
    /// entirely. Without a fallback that deadlocks — the app cannot run the engine,
    /// and only the engine rewrites the pointer — leaving the popover stuck on
    /// "could not refresh" until someone runs a scan by hand.
    static func resolve() -> [String]? {
        if let argv = recordedArgv(), FileManager.default.isExecutableFile(atPath: argv[0]) {
            return argv
        }
        guard let found = fallbackPaths.first(where: {
            FileManager.default.isExecutableFile(atPath: $0)
        }) else { return nil }
        // Record it, so the next launch does not have to search again.
        rewritePointer(to: found)
        return [found]
    }

    private static func recordedArgv() -> [String]? {
        guard let data = FileManager.default.contents(atPath: pointerPath()),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let argv = root["argv"] as? [String],
              !argv.isEmpty
        else { return nil }
        return argv
    }

    private static func rewritePointer(to executable: String) {
        guard let data = try? JSONSerialization.data(
            withJSONObject: ["argv": [executable]], options: [.prettyPrinted]
        ) else { return }
        let path = pointerPath()
        try? data.write(to: URL(fileURLWithPath: path))
        // Same posture as everything else this app touches: owner-only.
        try? FileManager.default.setAttributes(
            [.posixPermissions: 0o600], ofItemAtPath: path
        )
    }

    /// Run a scan, then call back on the main queue. Never throws into the UI:
    /// if the engine is missing or fails, the popover simply shows whatever the
    /// last successful scan produced.
    static func scan(completion: @escaping (Bool) -> Void) {
        guard let argv = resolve() else {
            DispatchQueue.main.async { completion(false) }
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: argv[0])
            process.arguments = Array(argv.dropFirst()) + ["scan", "--quiet"]
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice

            var ok = false
            do {
                try process.run()
                // A scan is incremental and typically a few milliseconds. The
                // cap stops a wedged run from leaving the popover spinning.
                let deadline = Date().addingTimeInterval(10)
                while process.isRunning && Date() < deadline {
                    usleep(20_000)
                }
                if process.isRunning { process.terminate() } else { ok = process.terminationStatus == 0 }
            } catch {
                ok = false
            }
            DispatchQueue.main.async { completion(ok) }
        }
    }
}
