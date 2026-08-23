import AppKit
import SwiftUI

/// A layout regression check, run in CI as `burn-o-meter --check-layout`.
///
/// This exists because a real bug shipped that every other check was blind to:
/// the popover had no `ScrollView`, so once a user accumulated enough models and
/// quota windows the content outgrew the screen and AppKit *clipped* it — from
/// the top, hiding the range picker and the headline while leaving the footer
/// visible. It compiled cleanly, the 42 engine tests passed, and CI's macOS job
/// was satisfied because the bundle built and set `LSUIElement`. Nothing looked
/// at the view.
///
/// The trap is that layout only breaks with *enough* data, so a developer's own
/// machine is the worst place to notice it. So the check renders the real view
/// against a deliberately oversized snapshot and measures it.
enum LayoutCheck {
    /// Every case must satisfy: the popover fits its ceiling, and is exactly the
    /// intended width. Removing the ScrollView makes `maximal` fail.
    /// Representative displays, smallest first. The popover must behave on all of
    /// them, not merely on whatever machine a contributor happens to use — the
    /// short ones are where an oversized layout actually bites.
    private static let screens: [(String, CGFloat)] = [
        ("11\" Air (768pt)", 768),
        ("12\" MacBook (800pt)", 800),
        ("13\" Air (900pt)", 900),
        ("14\" Pro (982pt)", 982),
        ("27\" external (1440pt)", 1440),
    ]

    static func run() -> Never {
        _ = NSApplication.shared
        var failures: [String] = []

        if let screen = NSScreen.main {
            print("  this machine: \(Int(screen.visibleFrame.height))pt usable")
        }

        for (name, snapshot) in cases() {
            let model = SnapshotModel()
            model.snapshot = snapshot
            let view = ContentView(
                model: model, onQuit: {}, onRefresh: {}, onOptions: {}
            )
            let natural = NSHostingView(rootView: view).fittingSize

            var problems: [String] = []
            if natural.height <= 0 || natural.width <= 0 {
                problems.append("degenerate size \(natural)")
            }
            // A long model name or project path must wrap, never widen the popover.
            if abs(natural.width - Theme.popoverWidth) > 1 {
                problems.append(
                    "width \(Int(natural.width)) != \(Int(Theme.popoverWidth)) — something "
                    + "inside is refusing to wrap"
                )
            }

            // On every display, the popover is min(content, ceiling) and must fit.
            for (label, visible) in screens {
                let ceiling = max(360, visible - 32)
                let shown = min(natural.height, ceiling)
                if shown > visible {
                    problems.append("\(label): would be \(Int(shown))pt on a \(Int(visible))pt screen")
                }
                if shown <= 0 {
                    problems.append("\(label): collapses to nothing")
                }
            }

            let smallest = screens[0].1
            let scrolls = natural.height > max(360, smallest - 32)
            print("  \(problems.isEmpty ? "✓" : "✗") \(name): \(Int(natural.width))x\(Int(natural.height))pt"
                  + (scrolls ? " — scrolls on the shortest display" : " — fits everywhere"))
            for p in problems { print("      \(p)") }
            if !problems.isEmpty { failures.append(name) }
        }

        // The menu bar is the other finite surface, and the one whose failure mode
        // is worst: macOS drops an item that does not fit without saying so, which
        // looks exactly like the app never launching. Measure the rendered width in
        // the real font rather than counting characters — an emoji is not one
        // character wide, and "~$44.05" is not the same width as "79%".
        let font = NSFont.menuBarFont(ofSize: 0)
        // Worst plausible case, not a lucky one: a two-digit percentage is as wide
        // as three, and a sum just under 100 keeps its cents, which is wider than a
        // rounded larger number. Measuring $289 would quietly flatter the result.
        var sample = synthetic(models: 3, projectsPerModel: 2, quotas: 3)
        sample.subtotalsByBasis = ["api_equivalent": 99.99]
        sample.quotas = [Quota(
            provider: "claude", window: "five_hour", usedPercent: 100,
            windowMinutes: 300, resetsAt: Date(), planType: "max20",
            isExact: true, ageSeconds: 60
        )]
        var widths: [MenuBarStyle: CGFloat] = [:]
        for style in MenuBarStyle.allCases {
            let title = sample.menuBarTitle(style: style)
            let w = (title as NSString).size(withAttributes: [.font: font]).width
            widths[style] = w
            print("  menu bar \(style.rawValue): \"\(title)\" \(Int(w))pt")
        }
        // A notched 13" laptop leaves very little once the system's own items are
        // placed. The default must be modest, not merely smaller than the maximum.
        // 120pt is about what a notched 13" bar can spare once the system's own
        // items are placed. Beyond that, the default risks being dropped silently.
        if let compact = widths[.compact], compact > 120 {
            print("      compact is \(Int(compact))pt — too greedy for a notched 13\" menu bar")
            failures.append("menu bar width")
        }
        if let minimal = widths[.minimal], minimal > 45 {
            print("      minimal is \(Int(minimal))pt — the always-fits option must stay tiny")
            failures.append("menu bar width")
        }
        if let full = widths[.full], let compact = widths[.compact], compact >= full {
            print("      compact (\(Int(compact))pt) is not narrower than full (\(Int(full))pt)")
            failures.append("menu bar width")
        }

        if failures.isEmpty {
            print("layout ok")
            exit(0)
        }
        print("layout FAILED: \(failures.joined(separator: ", "))")
        exit(1)
    }

    // MARK: - Cases

    private static func cases() -> [(String, Snapshot)] {
        [
            ("empty", Snapshot()),
            ("error", Snapshot(error: "the database could not be opened")),
            ("typical", synthetic(models: 3, projectsPerModel: 2, quotas: 3)),
            // The case the shipped bug needed: a heavy user, long model names, every
            // section present. This is what a developer's own machine rarely shows.
            ("maximal", synthetic(models: 12, projectsPerModel: 8, quotas: 6, longNames: true)),
        ]
    }

    private static func synthetic(
        models: Int, projectsPerModel: Int, quotas: Int, longNames: Bool = false
    ) -> Snapshot {
        var s = Snapshot()
        let name = { (i: Int) in
            longNames
                ? "claude-opus-5-with-an-unusually-long-provider-slug-\(i)"
                : "claude-opus-\(i)"
        }

        s.todayRows = (0..<models).map { i in
            ModelRow(
                model: name(i), provider: i.isMultiple(of: 2) ? "claude_code" : "codex",
                basis: .apiEquivalent, requests: 1234, costUSD: 281.98,
                totalTokens: 349_400_000, cacheHitRate: 0.979, effectiveRate: 0.81,
                priceSource: "overlay.toml@2026-08-21",
                projects: (0..<projectsPerModel).map { p in
                    ProjectSlice(
                        project: longNames ? "a-very-long-project-directory-name-\(p)" : "proj-\(p)",
                        requests: 120, costUSD: 12.34, shareOfModel: 0.25, cacheHitRate: 0.97
                    )
                }
            )
        }
        s.quotas = (0..<quotas).map { i in
            Quota(
                provider: i.isMultiple(of: 2) ? "claude" : "codex",
                window: "window_\(i)", usedPercent: Double(i * 13 % 100),
                windowMinutes: [300, 10080, 43200][i % 3],
                resetsAt: Date().addingTimeInterval(3600), planType: "max20",
                isExact: i.isMultiple(of: 2), ageSeconds: 60 * i
            )
        }
        s.currentWindow = UsageWindow(
            requests: 128, costUSD: 35.27, basis: .apiEquivalent,
            remainingSeconds: 11_400, relativeToMedian: 3.9, hasPublishedLimit: false
        )
        s.ranges = ["today", "month", "days30"].map { key in
            ChartRange(
                key: key, label: key.capitalized, bucket: key == "today" ? "hour" : "day",
                providers: ["claude_code", "codex"],
                points: (0..<30).map { p in
                    ChartPoint(
                        label: "\(p)",
                        byProvider: ["claude_code": Double(p) * 1.5, "codex": Double(p) * 0.3],
                        total: Double(p) * 1.8
                    )
                },
                subtotalsByBasis: ["api_equivalent": 288.64],
                cache: CacheStats(
                    hitRate: 0.97, cacheRead: 340_000_000, freshInput: 5_000_000,
                    cacheWrite: 4_400_000, savingsUSD: 174.0, actualUSD: 288.64,
                    withoutCacheUSD: 462.64
                )
            )
        }
        s.subtotalsByBasis = ["api_equivalent": 288.64]
        s.unpricedModels = longNames ? ["some-model-we-have-no-price-for"] : []
        s.generatedAt = Date()
        return s
    }
}
