import AppKit
import Foundation
import SwiftUI

/// A freshness regression check, run in CI as `burn-o-meter --check-freshness`.
///
/// Both bugs this guards against shipped, and neither showed up in a build, a
/// layout check, or any engine test.
///
/// A quota's age used to be read straight out of the payload's `age_seconds`,
/// which the writer computes once and never revises. When nothing rewrote the
/// snapshot — the normal case, since the background agent is optional and off by
/// default — that number stayed frozen, so a reading hours past its sampling
/// interval still classified as `current` and its percentage kept being shown as
/// a statement about now. Real usage moved on; the menu bar did not.
///
/// The paired failure was that nothing on the poll timer regenerated the payload
/// at all, so the only thing that advanced the number was opening the popover.
/// That part is asserted in `AppDelegate.tick()`; what is checked here is that
/// age tracks the wall clock, and that the classification degrades as it grows.
enum FreshnessCheck {
    static func run() -> Never {
        var failures: [String] = []

        func check(_ label: String, _ condition: Bool, _ detail: String = "") {
            if condition {
                print("  ✓ \(label)")
            } else {
                failures.append(label)
                print("  ✗ \(label)\(detail.isEmpty ? "" : " — \(detail)")")
            }
        }

        func quota(observedSecondsAgo: Double?, reported: Int?, percent: Double = 85) -> Quota {
            Quota(
                provider: "claude", window: "five_hour", usedPercent: percent,
                windowMinutes: 300, resetsAt: nil, planType: "max20", isExact: true,
                observedAt: observedSecondsAgo.map { Date().addingTimeInterval(-$0) },
                reportedAgeSeconds: reported
            )
        }

        print("freshness")

        // The bug itself: a reading observed long ago, in a payload whose writer
        // recorded it as one minute old. Age must come from the clock.
        let frozen = quota(observedSecondsAgo: 4 * 3600, reported: 60)
        check("age is derived from observed_at, not the payload's age_seconds",
              (frozen.ageSeconds ?? 0) > 3600,
              "got \(frozen.ageSeconds.map(String.init) ?? "nil")s, expected > 3600")
        check("a four-hour-old reading classifies as stale",
              frozen.freshness == .stale,
              "got \(frozen.freshness)")

        // ...and therefore cannot lead the menu bar.
        var snapshot = Snapshot()
        snapshot.quotas = [frozen]
        snapshot.subtotalsByBasis = ["api_equivalent": 12.34]
        check("a stale reading is not offered as the primary quota",
              snapshot.primaryQuota == nil)
        check("its percentage does not reach the menu bar",
              !snapshot.menuBarTitle(style: .full).contains("%"),
              "title was \"\(snapshot.menuBarTitle(style: .full))\"")

        // The classification boundaries, so a future edit cannot quietly widen them.
        check("under 5 minutes is current",
              quota(observedSecondsAgo: 120, reported: nil).freshness == .current)
        check("10 minutes is lagging, since the source samples every ~15",
              quota(observedSecondsAgo: 600, reported: nil).freshness == .lagging)
        check("45 minutes is stale",
              quota(observedSecondsAgo: 2701, reported: nil).freshness == .stale)

        // A fresh reading must still lead, including at the limit — the report was
        // that 100% never arrived, so the top of the range is asserted explicitly.
        var atLimit = Snapshot()
        atLimit.quotas = [quota(observedSecondsAgo: 60, reported: 60, percent: 100)]
        atLimit.subtotalsByBasis = ["api_equivalent": 12.34]
        check("100% reaches the menu bar",
              atLimit.menuBarTitle(style: .oneNumber).contains("100%"),
              "title was \"\(atLimit.menuBarTitle(style: .oneNumber))\"")

        // A payload with no observed_at at all still has to work, or an older
        // engine writing an older schema would show no quota rather than a lagging
        // one.
        let legacy = quota(observedSecondsAgo: nil, reported: 900)
        check("a payload without observed_at falls back to age_seconds",
              legacy.ageSeconds == 900 && legacy.freshness == .lagging,
              "age \(legacy.ageSeconds.map(String.init) ?? "nil"), \(legacy.freshness)")

        // Clock skew: a reading stamped in the future must not read as negative age
        // and must not be treated as anything other than current.
        let future = quota(observedSecondsAgo: -120, reported: nil)
        check("a future timestamp clamps to zero rather than going negative",
              future.ageSeconds == 0 && future.freshness == .current,
              "age \(future.ageSeconds.map(String.init) ?? "nil")")

        // The menu bar's nearly-exhausted colour. Pure function, so checked directly.
        print("menu bar limit colour")
        func range(_ text: String, _ percent: Double?, warn: Bool = true) -> NSRange? {
            MenuBarTitle.warningRange(in: text, percent: percent, warn: warn)
        }
        check("below the threshold stays uncoloured",
              range("89%  ~$44.05", 89) == nil)
        check("at the threshold, the reading is coloured",
              range("90%  ~$44.05", 90) == NSRange(location: 0, length: 3))
        check("at the limit, the whole reading is coloured",
              range("100%  ~$44.05", 100) == NSRange(location: 0, length: 4))
        // 89.6 displays as "90%". Colouring by the raw value would leave a figure
        // reading 90 uncoloured, disagreeing with itself.
        check("a reading that displays as 90% is coloured as 90%",
              range("90%  ~$44.05", 89.6) == NSRange(location: 0, length: 3),
              "range was \(String(describing: range("90%  ~$44.05", 89.6)))")
        check("only the percentage, never the spend beside it",
              range("~$44.05  95%", 95) == NSRange(location: 9, length: 3),
              "range was \(String(describing: range("~$44.05  95%", 95)))")
        check("switched off, nothing is coloured",
              range("95%  ~$44.05", 95, warn: false) == nil)
        check("a title without a reading is left alone",
              range("~$44.05", nil) == nil && range("", 95) == nil)

        // The colour is the popover's own for this level, not a second opinion.
        let title = MenuBarTitle.attributed("95%  ~$44.05", percent: 95, warn: true)
        let applied = title.attribute(.foregroundColor, at: 0, effectiveRange: nil) as? NSColor
        let spend = title.attribute(.foregroundColor, at: 5, effectiveRange: nil)
        let popover = NSColor(Theme.quotaState(95).color)
        // Components with a tolerance, not NSColor ==, which can differ on the float
        // representation of the same colour and would make this check flaky.
        func rgb(_ color: NSColor?) -> [CGFloat]? {
            guard let c = color?.usingColorSpace(.sRGB) else { return nil }
            return [c.redComponent, c.greenComponent, c.blueComponent]
        }
        let sameColour: Bool = {
            guard let a = rgb(applied), let b = rgb(popover) else { return false }
            return zip(a, b).allSatisfy { abs($0 - $1) < 0.01 }
        }()
        check("the colour matches the popover's for the same reading", sameColour,
              "menu bar \(String(describing: rgb(applied))) vs popover \(String(describing: rgb(popover)))")
        check("the spend keeps the button's own colour",
              spend == nil)
        // Right on the edge, where rounding changes the answer. 90 and 89.4 classify
        // the same way whether or not the value is rounded, so they cannot tell
        // whether the popover rounds at all - reverting its rounding passed every
        // other check here.
        check("the popover classifies a reading shown as 90% as nearly exhausted",
              Theme.quotaState(89.6).label == "nearly exhausted",
              "89.6 was \"\(Theme.quotaState(89.6).label)\"")
        check("the popover and the menu bar agree on where the threshold is",
              Theme.quotaState(90).label == "nearly exhausted"
                  && Theme.quotaState(89.4).label == "getting full"
                  && Theme.isNearlyExhausted(90) && !Theme.isNearlyExhausted(89.4))

        if failures.isEmpty {
            print("freshness ok")
            exit(0)
        }
        print("freshness FAILED: \(failures.count) of the above")
        exit(1)
    }
}
