import AppKit
import ServiceManagement

/// App-level settings that live outside the engine's config file.
///
/// Deliberately thin. Anything affecting *numbers* — privacy mode, billing
/// basis, pricing overrides — belongs in `~/.burn-o-meter/config.toml` where the
/// CLI reads it too; duplicating that here would create two sources of truth for
/// the same setting. What lives here is only what the app itself owns.
/// How much the status item shows.
///
/// The menu bar is shared, finite, and on a notched laptop much smaller than it
/// looks — the notch splits it, and macOS silently drops whatever no longer fits
/// rather than telling anyone. A title like "🔥 79% · ~$44.05" is 15 characters,
/// which is a lot to claim from a space the user has other plans for, and the
/// failure mode is the app appearing not to launch at all.
enum MenuBarStyle: String, CaseIterable {
    /// Both numbers, to the cent.
    case full
    /// Both numbers, tightened: the cents rounded away and the separator dropped.
    /// Keeps the information and gives back roughly a third of the width, which is
    /// usually the difference between fitting and not.
    case compact
    /// The single most useful number — quota on a subscription, spend on an API key.
    case oneNumber
    /// Flame only. Always fits; the numbers are one click away.
    case minimal

    var label: String {
        switch self {
        case .full: return "Percentage and Spend"
        case .compact: return "Percentage and Spend (Compact)"
        case .oneNumber: return "One Number"
        case .minimal: return "Icon Only"
        }
    }
}

enum Preferences {

    /// Defaults to `compact`: both numbers, tightened. A default should work on the
    /// smallest common setup rather than the developer's, since an invisible menu
    /// bar item reads as a broken app — but dropping to one number gives up more
    /// than it needs to. macOS offers no way to detect that an item did not fit, so
    /// this cannot adapt on its own; the gear menu switches it in one click.
    static var menuBarStyle: MenuBarStyle {
        get {
            UserDefaults.standard.string(forKey: "menuBarStyle")
                .flatMap(MenuBarStyle.init(rawValue:)) ?? .compact
        }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: "menuBarStyle") }
    }


    // MARK: Launch at login

    /// Registered through SMAppService, so macOS owns the login item and the
    /// user can revoke it in System Settings → General → Login Items. The older
    /// approach — writing a LaunchAgent plist ourselves — leaves an entry the
    /// system UI cannot manage.
    static var launchAtLogin: Bool {
        get { SMAppService.mainApp.status == .enabled }
        set {
            do {
                if newValue {
                    if SMAppService.mainApp.status != .enabled {
                        try SMAppService.mainApp.register()
                    }
                } else if SMAppService.mainApp.status == .enabled {
                    try SMAppService.mainApp.unregister()
                }
            } catch {
                NSLog("burn-o-meter: could not change login item: \(error.localizedDescription)")
            }
        }
    }

    /// True when macOS has the login item registered but the user switched it
    /// off in System Settings. Worth distinguishing: re-registering will not
    /// help, and the app should say so rather than silently failing.
    static var launchAtLoginBlockedByUser: Bool {
        SMAppService.mainApp.status == .requiresApproval
    }

    /// True when macOS will not register this app as a login item at all.
    ///
    /// Happens when the bundle is not in `/Applications` — running from a build
    /// directory reports `.notFound`, and no amount of registering will change
    /// that. Worth distinguishing from "off", because the fix is to move the
    /// app, not to click the toggle again.
    static var launchAtLoginUnavailable: Bool {
        SMAppService.mainApp.status == .notFound
    }

    static var isInApplicationsFolder: Bool {
        Bundle.main.bundlePath.hasPrefix("/Applications/")
    }

    // MARK: Background scanning

    /// Whether the launchd scan agent is installed.
    ///
    /// The app does not install it itself: the agent runs the *engine*, and
    /// wiring one lifecycle into the other would leave the agent pointing at a
    /// stale path whenever the CLI moved. The app reports state and defers to
    /// `burn-o-meter agent install`.
    static var backgroundScanningInstalled: Bool {
        FileManager.default.fileExists(atPath: agentPlistPath)
    }

    static var agentPlistPath: String {
        NSHomeDirectory() + "/Library/LaunchAgents/com.burn-o-meter.scan.plist"
    }

    static var dataDirectory: String {
        if let override = ProcessInfo.processInfo.environment["BURNOMETER_HOME"] {
            return override
        }
        return NSHomeDirectory() + "/.burn-o-meter"
    }

    /// Login-item state as a readable string.
    ///
    /// Exposed so it can be verified without privilege. `sfltool dumpbtm` reads
    /// the system Background Task Management database and needs sudo;
    /// SMAppService answers the same question about *this* app for free.
    static var launchAtLoginStatus: String {
        switch SMAppService.mainApp.status {
        case .enabled: return "enabled"
        case .notRegistered: return "not registered"
        case .requiresApproval: return "blocked by user in System Settings"
        case .notFound: return "not found"
        @unknown default: return "unknown"
        }
    }

    static var version: String {
        Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "dev"
    }
}
