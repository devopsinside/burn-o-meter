import Foundation
import SwiftUI

/// How a dollar figure should be read. Mirrors `burnometer.models.CostBasis`.
enum CostBasis: String {
    case apiBilled = "api_billed"
    case apiEquivalent = "api_equivalent"
    case unpriced = "unpriced"
    case notMetered = "not_metered"

    /// Subscription figures are counterfactual, so they always carry a tilde.
    var prefix: String { self == .apiEquivalent ? "~" : "" }

    var explanation: String {
        switch self {
        case .apiBilled: return "billed per token against an API key"
        case .apiEquivalent: return "subscription — not billed per token; API-equivalent value"
        case .unpriced: return "no published rate for these models"
        case .notMetered: return "not billed per token — self-hosted, or covered by a plan"
        }
    }
}

/// Where one model's spend went.
struct ProjectSlice: Identifiable {
    let project: String
    let requests: Int
    let costUSD: Double?
    let shareOfModel: Double?
    let cacheHitRate: Double?

    var id: String { project }
}

struct ModelRow: Identifiable {
    let model: String
    let provider: String
    let basis: CostBasis
    let requests: Int
    let costUSD: Double?
    let totalTokens: Int
    let cacheHitRate: Double?
    let effectiveRate: Double?
    /// Where the rate came from, e.g. "models.dev@2026-08-21+overlay(cache_write_1h)".
    let priceSource: String?
    /// Per-project breakdown, revealed when the row is expanded.
    let projects: [ProjectSlice]

    var id: String { "\(provider)/\(model)/\(basis.rawValue)" }
}

struct DayCost: Identifiable {
    let day: String
    let costUSD: Double
    var id: String { day }
}

struct ChartPoint {
    let label: String
    let byProvider: [String: Double]
    let total: Double
}

/// One selectable view of the same measure.
/// How much of the prompt side came from cache, and what that was worth.
struct CacheStats {
    let hitRate: Double?
    let cacheRead: Int
    let freshInput: Int
    let cacheWrite: Int
    let savingsUSD: Double?
    let actualUSD: Double?
    /// What the same work would have cost with no prompt caching at all.
    let withoutCacheUSD: Double?

    /// Share of the uncached cost that caching avoided.
    var savedFraction: Double? {
        guard let saved = savingsUSD, let without = withoutCacheUSD, without > 0 else { return nil }
        return saved / without
    }

    /// Cache efficiency is a "higher is better" measure, so the thresholds run
    /// the opposite way to a quota. In agentic use 90%+ is normal; below about
    /// 60% something is invalidating the prefix on nearly every request.
    var state: (color: Color, label: String) {
        guard let rate = hitRate else { return (.secondary, "no data") }
        switch rate {
        case 0.85...: return (Theme.good, "excellent")
        case 0.60..<0.85: return (Theme.warning, "moderate")
        default: return (Theme.serious, "poor — prefix keeps changing")
        }
    }
}

struct ChartRange: Identifiable {
    let key: String
    let label: String
    let bucket: String
    let providers: [String]
    let points: [ChartPoint]
    let subtotalsByBasis: [String: Double]
    let cache: CacheStats

    var id: String { key }

    var subtotals: [(CostBasis, Double)] {
        subtotalsByBasis
            .compactMap { key, value in CostBasis(rawValue: key).map { ($0, value) } }
            .sorted { $0.1 > $1.1 }
    }

    var peak: Double { points.map(\.total).max() ?? 0 }

    /// Axis caption. Says what a bar is, so the chart needs no axis labels.
    var bucketLabel: String { bucket == "hour" ? "per hour" : "per day" }
}

struct Quota: Identifiable {
    let provider: String
    let window: String
    let usedPercent: Double?
    let windowMinutes: Int
    let resetsAt: Date?
    let planType: String?
    let isExact: Bool
    /// How old the reading is. Claude's figures are only written while the
    /// desktop app runs, so one can be hours stale — and a stale percentage
    /// presented as current is worse than none.
    let ageSeconds: Int?

    var id: String { "\(provider)/\(window)" }

    /// "go" -> "Plan: Go". Providers report these lowercase; rendering them raw
    /// reads like a typo next to everything else in the row.
    var planLabel: String? {
        guard let plan = planType, !plan.isEmpty else { return nil }
        let known = ["go": "Go", "plus": "Plus", "pro": "Pro", "max": "Max",
                     "team": "Team", "enterprise": "Enterprise", "free": "Free",
                     "max5": "Max 5x", "max20": "Max 20x"]
        let pretty = known[plan.lowercased()] ?? plan.prefix(1).uppercased() + plan.dropFirst()
        return "Plan: \(pretty)"
    }

    var windowLabel: String {
        let days = Double(windowMinutes) / 1440
        if days >= 1 { return "\(Int(days.rounded()))-day" }
        return "\(windowMinutes / 60)-hour"
    }

    /// How much to trust this reading as a statement about *now*.
    ///
    /// Claude's figures are written by the desktop app roughly every 15 minutes,
    /// so a reading is routinely several minutes behind what that app shows
    /// live. That is a property of the source, not a fault, but it means a bare
    /// percentage can quietly misrepresent the present — hence three levels
    /// rather than a fresh/stale flip.
    enum Freshness {
        case current    // within a sampling interval; show it plainly
        case lagging    // behind, but still meaningful; annotate with its age
        case stale      // several intervals missed; draw it back
    }

    var freshness: Freshness {
        switch ageSeconds ?? Int.max {
        case ..<300: return .current
        case ..<2700: return .lagging
        default: return .stale
        }
    }

    var isStale: Bool { freshness == .stale }

    /// Providers that only sample periodically, so the UI can say why a reading
    /// lags instead of leaving the reader to assume it is broken.
    var isPeriodicallySampled: Bool { provider == "claude" }

    var title: String {
        switch (provider, window) {
        case ("claude", "five_hour"): return "Claude · 5-hour window"
        case ("claude", "seven_day"): return "Claude · weekly"
        case ("codex", "primary"): return "Codex · \(windowLabel) window"
        case ("codex", "secondary"): return "Codex · secondary"
        default: return "\(provider) \(window)"
        }
    }

    /// The window on its own, for use under a provider group header.
    var windowTitle: String {
        switch (provider, window) {
        case (_, "five_hour"): return "5-hour window"
        case (_, "seven_day"): return "weekly"
        case ("codex", "primary"): return "\(windowLabel) window"
        case ("codex", "secondary"): return "secondary window"
        default: return windowLabel
        }
    }

    /// Ordering for display: the window most likely to interrupt work first.
    var priority: Int {
        switch (provider, window) {
        case ("claude", "five_hour"): return 0
        case ("codex", "primary"): return 1
        case ("claude", "seven_day"): return 2
        default: return 3
        }
    }
}

/// A rolling usage window.
///
/// Carries no `usedPercent`, and that is deliberate. Anthropic publishes no
/// token limit for subscription plans and Claude Code stores no quota on disk,
/// so a percentage here could only be invented. `relativeToMedian` compares the
/// window against the user's own history instead — a claim we can support.
struct UsageWindow {
    let requests: Int
    let costUSD: Double?
    let basis: CostBasis
    let remainingSeconds: Int
    let relativeToMedian: Double?

    /// Always false today. Kept explicit so the absence of a percentage reads as
    /// a decision rather than an oversight: Anthropic publishes no token limit
    /// for subscription plans, so one could only be invented.
    let hasPublishedLimit: Bool

    var remaining: TimeInterval { TimeInterval(max(remainingSeconds, 0)) }
}

/// Everything the popover needs, assembled in one pass off the main thread.
struct Snapshot {
    var todayRows: [ModelRow] = []
    var dailyCosts: [DayCost] = []
    var ranges: [ChartRange] = []
    var currentWindow: UsageWindow?
    var quotas: [Quota] = []
    var generatedAt: Date?
    var unpricedModels: [String] = []
    var basisNotes: [String: String] = [:]
    var error: String?

    /// Subtotals as the engine computed them, keyed by basis. There is no
    /// combined total, on purpose: adding a real charge to an API-equivalent
    /// figure produces a number that means nothing.
    var subtotalsByBasis: [String: Double] = [:]

    init(error: String? = nil) { self.error = error }

    var subtotals: [(CostBasis, Double)] {
        subtotalsByBasis
            .compactMap { key, value in
                CostBasis(rawValue: key).map { ($0, value) }
            }
            .sorted { $0.1 > $1.1 }
    }

    func note(for basis: CostBasis) -> String {
        basisNotes[basis.rawValue] ?? basis.explanation
    }

    /// True when the engine has not run recently enough for these numbers to be
    /// presented as current.
    var isStale: Bool {
        guard let generatedAt else { return true }
        return Date().timeIntervalSince(generatedAt) > 300
    }

    /// The menu bar title: the most useful thing that fits in a few characters.
    ///
    /// Spend alone answers "what did today cost". It does not answer "can I keep
    /// going", which is the question that actually interrupts work — so an exact
    /// quota reading is appended when one exists, and it leads once it is high
    /// enough to matter.
    /// The quota reading most worth glancing at: the shortest live window,
    /// preferring one that is actually current.
    var primaryQuota: Quota? {
        quotas
            .filter { $0.usedPercent != nil && !$0.isStale }
            .min { $0.priority < $1.priority }
    }

    /// The menu bar title.
    ///
    /// Which number leads depends on how the user pays. On a subscription the
    /// binding constraint is the rolling window — "can I keep going" is the
    /// question that interrupts work, and spend is informational. On an API key
    /// there is no window to run out of, so cost leads.
    var menuBarTitle: String { menuBarTitle(style: Preferences.menuBarStyle) }

    /// - Parameter style: how much to show. The menu bar is finite and shared, and
    ///   macOS drops an item that no longer fits without saying so — which looks
    ///   exactly like the app failing to launch. See `MenuBarStyle`.
    func menuBarTitle(style: MenuBarStyle) -> String {
        if error != nil { return "🔥 ⚠️" }
        if style == .minimal { return "🔥" }

        let spend = subtotals.first.map { "\($0.0.prefix)\(Format.money($0.1))" }
        let onSubscription = subtotals.first?.0 == .apiEquivalent

        if let quota = primaryQuota, let percent = quota.usedPercent {
            let pct = "\(Int(percent.rounded()))%"
            guard let spend else { return "🔥 \(pct)" }
            switch style {
            case .full:
                return onSubscription ? "🔥 \(pct) · \(spend)" : "🔥 \(spend) · \(pct)"
            case .compact:
                // Cents in a menu bar are noise: nobody acts on the difference
                // between $47.97 and $48, and the two characters cost real width.
                let tight = subtotals.first.map { "\($0.0.prefix)\(Format.moneyTight($0.1))" } ?? ""
                return onSubscription ? "🔥 \(pct) \(tight)" : "🔥 \(tight) \(pct)"
            case .oneNumber:
                // Which number survives depends on how the user pays: on a
                // subscription the window is the binding constraint, on an API key
                // there is no window to exhaust and only cost means anything.
                return onSubscription ? "🔥 \(pct)" : "🔥 \(spend)"
            case .minimal:
                return "🔥"
            }
        }
        return spend.map { "🔥 \($0)" } ?? "🔥 —"
    }
}

enum Format {
    /// Money for the menu bar, where width is the scarce resource.
    static func moneyTight(_ usd: Double) -> String {
        usd >= 10 ? String(format: "$%.0f", usd) : String(format: "$%.1f", usd)
    }

    static func money(_ usd: Double) -> String {
        usd >= 100 ? String(format: "$%.0f", usd) : String(format: "$%.2f", usd)
    }

    /// An unpriced row shows a dash, never `$0.00` — "free" and "unknown" are
    /// different facts, and conflating them understates a bill.
    static func cost(_ usd: Double?, _ basis: CostBasis) -> String {
        guard let usd else { return "—" }
        return basis.prefix + money(usd)
    }

    static func tokens(_ n: Int) -> String {
        let value = Double(n)
        if value >= 1_000_000_000 { return String(format: "%.2fB", value / 1_000_000_000) }
        if value >= 1_000_000 { return String(format: "%.1fM", value / 1_000_000) }
        if value >= 1_000 { return String(format: "%.1fK", value / 1_000) }
        return "\(n)"
    }

    static func percent(_ value: Double?) -> String {
        guard let value else { return "—" }
        return String(format: "%.0f%%", value * 100)
    }

    /// Compact duration. Rolls up to days past two days, because "791h35m" is
    /// a number a reader has to decode rather than read.
    static func duration(_ interval: TimeInterval) -> String {
        let minutes = Int(interval) / 60
        if minutes < 60 { return "\(minutes)m" }
        let hours = minutes / 60
        if hours < 48 { return "\(hours)h\(String(format: "%02d", minutes % 60))m" }
        let days = hours / 24
        return days < 14 ? "\(days)d" : "\(days / 7)w"
    }

    static func ago(_ date: Date?) -> String {
        guard let date else { return "never" }
        let seconds = Int(Date().timeIntervalSince(date))
        if seconds < 60 { return "\(max(seconds, 0))s ago" }
        if seconds < 3600 { return "\(seconds / 60)m ago" }
        return "\(seconds / 3600)h ago"
    }
}
