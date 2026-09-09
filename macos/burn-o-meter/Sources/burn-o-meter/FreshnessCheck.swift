import Foundation

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

        if failures.isEmpty {
            print("freshness ok")
            exit(0)
        }
        print("freshness FAILED: \(failures.count) of the above")
        exit(1)
    }
}
