import AppKit
import Combine
import SwiftUI

/// The data the popover renders, published so SwiftUI can update in place.
///
/// This exists because of a real bug: an earlier version rebuilt the popover's
/// `NSHostingController` on every refresh. Swapping the content view controller
/// of a *shown* NSPopover makes it re-layout from the new controller's initial
/// intrinsic size, which collapses the popover to roughly nothing a second or
/// two after opening. The controller is now created once and only its published
/// state changes.
final class SnapshotModel: ObservableObject {
    @Published var snapshot = Snapshot()
    @Published var isRefreshing = false
}

final class AppDelegate: NSObject, NSApplicationDelegate {

    private var statusItem: NSStatusItem!
    private let popover = NSPopover()
    private let model = SnapshotModel()
    private var timer: Timer?

    /// Built once and reused. Rebuilding an NSMenu on each click loses the
    /// highlight state the system draws while it is open.
    private lazy var optionsMenu = makeOptionsMenu()

    /// Polling a small local JSON file. Cheap enough to feel live; the freshness
    /// of the *data* is set by the scan interval, not by this.
    private let pollInterval: TimeInterval = 2

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.action = #selector(statusItemClicked)
        statusItem.button?.target = self
        // Right-click opens the options menu; left-click opens the popover.
        statusItem.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])

        // Set synchronously. With `variableLength` and no title or image the
        // button has zero width and is invisible, which looks exactly like the
        // app failing to launch.
        statusItem.button?.title = "🔥 …"
        statusItem.button?.toolTip = "burn-o-meter"

        popover.behavior = .transient
        popover.animates = false
        // Built once, then left alone.
        popover.contentViewController = NSHostingController(
            rootView: ContentView(
                model: model,
                onQuit: { NSApp.terminate(nil) },
                onRefresh: { [weak self] in self?.refresh() },
                onOptions: { [weak self] in self?.showOptionsMenu() },
                onCheckUpdates: { [weak self] in self?.checkForUpdates() }
            )
        )

        // Plugging in a monitor, unplugging one, or changing resolution all change
        // how tall the popover may be. Without this it keeps a ceiling computed for
        // a screen that is no longer there.
        NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification,
            object: nil, queue: .main
        ) { [weak self] _ in
            guard let self, self.popover.isShown else { return }
            self.sizePopoverToContent()
        }

        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: pollInterval, repeats: true) { [weak self] _ in
            self?.reload()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate()
    }

    /// Re-read the payload the engine wrote. Cheap; runs on the poll timer.
    private func reload() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let fresh = SnapshotFile.load()
            DispatchQueue.main.async {
                guard let self else { return }
                self.model.snapshot = fresh
                self.statusItem.button?.title = fresh.menuBarTitle
                // New data changes the content's height. Without this the popover
                // keeps whatever size it had when it opened, which is how it ends
                // up cut off mid-section.
                if self.popover.isShown {
                    DispatchQueue.main.async { self.sizePopoverToContent() }
                }
            }
        }
    }

    /// Ask the engine to scan, then re-read. Runs on launch and whenever the
    /// popover opens, so what the user sees is current rather than whenever the
    /// background agent last happened to fire.
    private func refresh() {
        model.isRefreshing = true
        Engine.scan { [weak self] _ in
            guard let self else { return }
            self.model.isRefreshing = false
            self.reload()
        }
    }

    // MARK: - Options menu

    @objc private func statusItemClicked() {
        let isRightClick = NSApp.currentEvent?.type == .rightMouseUp
            || NSApp.currentEvent?.modifierFlags.contains(.control) == true
        if isRightClick {
            showOptionsMenu()
        } else {
            togglePopover()
        }
    }

    @objc func showOptionsMenu() {
        if popover.isShown { popover.performClose(nil) }
        refreshOptionsMenuState()
        // Attaching the menu to the item makes the system draw it in the right
        // place and highlight the status item while it is open.
        statusItem.menu = optionsMenu
        statusItem.button?.performClick(nil)
        statusItem.menu = nil
    }

    private func makeOptionsMenu() -> NSMenu {
        let menu = NSMenu()

        let launch = NSMenuItem(title: "Launch at Login",
                                action: #selector(toggleLaunchAtLogin), keyEquivalent: "")
        launch.target = self
        menu.addItem(launch)

        // Menu bar width is the scarcest resource this app uses, and the user is
        // the only one who knows how much of it they can spare.
        let display = NSMenuItem(title: "Menu Bar Shows", action: nil, keyEquivalent: "")
        let displayMenu = NSMenu()
        for style in MenuBarStyle.allCases {
            let item = NSMenuItem(title: style.label,
                                  action: #selector(setMenuBarStyle(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = style.rawValue
            displayMenu.addItem(item)
        }
        display.submenu = displayMenu
        menu.addItem(display)

        let scanning = NSMenuItem(title: "Background Scanning", action: nil, keyEquivalent: "")
        scanning.isEnabled = false
        menu.addItem(scanning)

        menu.addItem(.separator())

        let scanNow = NSMenuItem(title: "Scan Now", action: #selector(scanNow), keyEquivalent: "r")
        scanNow.target = self
        menu.addItem(scanNow)

        let reveal = NSMenuItem(title: "Reveal Data Folder",
                                action: #selector(revealDataFolder), keyEquivalent: "")
        reveal.target = self
        menu.addItem(reveal)

        menu.addItem(.separator())

        // Says "Check" rather than offering a toggle, because there is nothing to
        // toggle: it only ever runs from here.
        let updates = NSMenuItem(title: "Check for Updates…",
                                 action: #selector(checkForUpdates), keyEquivalent: "")
        updates.target = self
        menu.addItem(updates)

        menu.addItem(.separator())

        let about = NSMenuItem(title: "burn-o-meter", action: nil, keyEquivalent: "")
        about.isEnabled = false
        menu.addItem(about)

        let quit = NSMenuItem(title: "Quit", action: #selector(NSApp.terminate(_:)),
                              keyEquivalent: "q")
        menu.addItem(quit)
        return menu
    }

    /// Menu items reflect live system state, so they are refreshed each time the
    /// menu opens rather than set once at build.
    private func refreshOptionsMenuState() {
        // Tick whichever menu bar style is active.
        if let display = optionsMenu.item(withTitle: "Menu Bar Shows")?.submenu {
            for item in display.items {
                let raw = item.representedObject as? String
                item.state = raw == Preferences.menuBarStyle.rawValue ? .on : .off
            }
        }
        // The item is rebuilt by title each time, so find it by prefix.
        if let launch = optionsMenu.items.first(where: { $0.title.hasPrefix("Launch at Login") }) {
            launch.state = Preferences.launchAtLogin ? .on : .off
            launch.isEnabled = true
            launch.title = "Launch at Login"
            if Preferences.launchAtLoginUnavailable {
                // Clicking again cannot fix this — the bundle has to move.
                launch.title = "Launch at Login — move app to /Applications first"
                launch.isEnabled = false
            } else if Preferences.launchAtLoginBlockedByUser {
                // System Settings has the final say; re-registering will not help.
                launch.title = "Launch at Login — blocked in System Settings"
                launch.isEnabled = false
            }
        }
        if let scanning = optionsMenu.item(withTitle: "Background Scanning") {
            scanning.title = Preferences.backgroundScanningInstalled
                ? "Background Scanning: on (every 60s)"
                : "Background Scanning: off — run `burn-o-meter agent install`"
        }
        if let about = optionsMenu.items.first(where: { $0.title.hasPrefix("burn-o-meter") }) {
            about.title = "burn-o-meter \(Preferences.version)"
        }
    }

    @objc private func checkForUpdates() {
        Updates.check(from: statusItem.button)
    }

    @objc private func setMenuBarStyle(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String,
              let style = MenuBarStyle(rawValue: raw) else { return }
        Preferences.menuBarStyle = style
        statusItem.button?.title = model.snapshot.menuBarTitle
    }

    @objc private func toggleLaunchAtLogin() {
        Preferences.launchAtLogin.toggle()
    }

    @objc private func scanNow() {
        refresh()
    }

    @objc private func revealDataFolder() {
        NSWorkspace.shared.selectFile(
            Preferences.dataDirectory + "/burn.db",
            inFileViewerRootedAtPath: Preferences.dataDirectory
        )
    }

    /// Tell the popover how tall to be, rather than letting it infer.
    ///
    /// NSPopover sizes itself from its content view controller. That worked while
    /// the root view had an intrinsic height — until a heavy user's content grew
    /// past the screen, at which point AppKit clipped it from the top instead of
    /// scrolling. Wrapping the content in a ScrollView fixed the clipping but took
    /// the intrinsic height away with it, so the popover had nothing to size from
    /// and settled far too small. Both symptoms share one cause: nobody ever told
    /// the popover how big to be. Now somebody does.
    ///
    /// `fittingSize` still reports the content's natural height through the
    /// ScrollView, so the popover is exactly as tall as its content needs and only
    /// reaches the ceiling — and therefore only scrolls — when it cannot fit.
    private func sizePopoverToContent() {
        guard let host = popover.contentViewController?.view else { return }
        let natural = host.fittingSize.height
        guard natural > 0 else { return }
        // The display the menu bar icon is on — which is not necessarily the main
        // one when an external monitor is attached.
        let screen = statusItem?.button?.window?.screen ?? NSScreen.main
        popover.contentSize = NSSize(
            width: Theme.popoverWidth,
            height: min(natural, Theme.popoverMaxHeight(on: screen))
        )
    }

    /// Report whether macOS is actually giving the status item room.
    ///
    /// The question this answers: when the menu bar is full, is there any signal we
    /// can read? If there is, the title can shrink itself instead of vanishing.
    func probeMenuBarAndExit() {
        var delay = 0.0
        for style in MenuBarStyle.allCases {
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
                Preferences.menuBarStyle = style
                let title = self.model.snapshot.menuBarTitle(style: style)
                self.statusItem.button?.title = title
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
                    let button = self.statusItem.button
                    let frame = button?.window?.frame ?? .zero
                    let screenW = button?.window?.screen?.frame.width ?? -1
                    print("\(style.rawValue): \"\(title)\" button=\(Int(button?.frame.width ?? -1))pt"
                          + " windowX=\(Int(frame.origin.x)) windowW=\(Int(frame.width))"
                          + " visible=\(self.statusItem.isVisible) screenW=\(Int(screenW))")
                }
            }
            delay += 1.4
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + delay + 1.0) { exit(0) }
    }

    /// Drive the real popover once and report what it actually became.
    ///
    /// Offline measurement could not settle this: `preferredContentSize` is 0
    /// until the view is in a window, and `NSPopover.contentSize` is 0 until it is
    /// shown. So the probe shows it for real, reads the geometry, and exits — the
    /// only measurement that reflects what a user sees.
    func probePopoverAndExit() {
        togglePopover()
        DispatchQueue.main.asyncAfter(deadline: .now() + (CommandLine.arguments.contains("--hold") ? 8.0 : 1.2)) {
            let content = self.popover.contentSize
            let windowHeight = self.popover.contentViewController?.view.window?.frame.height ?? -1
            let natural = self.popover.contentViewController?.view.fittingSize.height ?? -1
            let ceiling = Theme.popoverMaxHeight
            print("shown:    \(Int(content.width)) x \(Int(content.height))")
            print("window:   \(Int(windowHeight))pt")
            print("natural:  \(Int(natural))pt")
            print("ceiling:  \(Int(ceiling))pt")

            var bad: [String] = []
            if content.height < 1 { bad.append("popover has no height") }
            if natural > 1, content.height < min(natural, ceiling) - 2 {
                bad.append("showing \(Int(content.height))pt of \(Int(min(natural, ceiling)))pt — content is cut off")
            }
            if abs(content.width - Theme.popoverWidth) > 1 {
                bad.append("width \(Int(content.width)) != \(Int(Theme.popoverWidth))")
            }
            if bad.isEmpty {
                print("popover ok")
                exit(0)
            }
            for b in bad { print("FAIL: \(b)") }
            exit(1)
        }
    }

    @objc private func togglePopover() {
        if popover.isShown {
            popover.performClose(nil)
            return
        }
        guard let button = statusItem.button else { return }
        // Scan on open: a meter should show what is true now, not what was true
        // the last time a timer fired.
        refresh()
        sizePopoverToContent()
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
    }
}

/// Headless self-check: print what the UI would show, as JSON, and exit.
///
/// Exists so the Swift side can be verified against `burn-o-meter today --json`
/// without launching a GUI — the two must agree, since disagreement would mean
/// the menu bar is quietly showing different numbers from the CLI.
func dumpSnapshotAndExit() -> Never {
    let snapshot = SnapshotFile.load()
    var payload: [String: Any] = [
        "menu_bar_title": snapshot.menuBarTitle,
        "stale": snapshot.isStale,
        "subtotals": snapshot.subtotals.reduce(into: [String: Double]()) { $0[$1.0.rawValue] = $1.1 },
        "rows": snapshot.todayRows.map { row in
            [
                "model": row.model,
                "provider": row.provider,
                "cost_basis": row.basis.rawValue,
                "requests": row.requests,
                "cost_usd": row.costUSD as Any,
                "total_tokens": row.totalTokens,
                "cache_hit_rate": row.cacheHitRate as Any,
                "effective_rate_usd_per_mtok": row.effectiveRate as Any,
            ] as [String: Any]
        },
        "quotas": snapshot.quotas.map { quota in
            [
                "provider": quota.provider,
                "window": quota.window,
                "used_percent": quota.usedPercent as Any,
                "exact": quota.isExact,
            ] as [String: Any]
        },
        "unpriced_models": snapshot.unpricedModels,
        "app": [
            "version": Preferences.version,
            "launch_at_login": Preferences.launchAtLoginStatus,
            "background_scanning": Preferences.backgroundScanningInstalled,
            "data_directory": Preferences.dataDirectory,
        ] as [String: Any],
    ]
    if let window = snapshot.currentWindow {
        payload["current_window"] = [
            "requests": window.requests,
            "cost_usd": window.costUSD as Any,
            "remaining_seconds": window.remainingSeconds,
            "relative_to_median": window.relativeToMedian as Any,
        ] as [String: Any]
    }
    if let error = snapshot.error { payload["error"] = error }

    if let data = try? JSONSerialization.data(withJSONObject: payload,
                                              options: [.prettyPrinted, .sortedKeys]),
       let text = String(data: data, encoding: .utf8) {
        print(text)
    }
    exit(0)
}

if CommandLine.arguments.contains("--dump") {
    dumpSnapshotAndExit()
}

// Layout regression check, run by CI. See LayoutCheck for why it exists.
if CommandLine.arguments.contains("--check-layout") {
    LayoutCheck.run()
}

let probingPopover = CommandLine.arguments.contains("--probe-popover")
let probingMenuBar = CommandLine.arguments.contains("--probe-menubar")

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate

if probingMenuBar {
    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
        delegate.probeMenuBarAndExit()
    }
}

if probingPopover {
    // Let the delegate finish launching, then drive the real popover once.
    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
        delegate.probePopoverAndExit()
    }
}
// Accessory: no Dock icon, no menu bar menu — it lives entirely in the status bar.
app.setActivationPolicy(.accessory)
app.run()
