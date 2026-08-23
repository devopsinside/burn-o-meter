import AppKit
import SwiftUI

/// Colors and spacing.
///
/// Every hue below comes from a palette validated for colorblind separation and
/// contrast against both surfaces, rather than picked by eye. Two rules from
/// that system are load-bearing here:
///
/// * **Identity color follows the entity, never its rank.** Rows are ordered by
///   spend, so colouring by position would repaint a model the moment it moved.
///   The dot is coloured by *provider* instead, which is stable and is also the
///   distinction worth seeing at a glance.
/// * **Status colors are reserved and never carry meaning alone.** Quota state
///   always ships with a number and a label beside the colour, because the
///   warning step is deliberately sub-3:1 on the light surface.
enum Theme {

    // MARK: Palette

    /// Categorical slot 1 (blue) — stepped separately for each surface.
    static let series1 = dynamic(light: 0x2A78D6, dark: 0x3987E5)
    /// Categorical slot 2 (orange).
    static let series2 = dynamic(light: 0xEB6834, dark: 0xD95926)

    /// Status steps are fixed across modes on purpose: they must never drift
    /// into a categorical slot and start impersonating a series.
    static let good = Color(hex: 0x0CA30C)
    static let warning = Color(hex: 0xFAB219)
    static let serious = Color(hex: 0xEC835A)
    static let critical = Color(hex: 0xD03B3B)

    /// Sequential blue, used for magnitude (share bars). One hue, light→dark.
    static let magnitude = dynamic(light: 0x2A78D6, dark: 0x3987E5)
    static let magnitudeTrack = dynamic(light: 0xCDE2FB, dark: 0x184F95)

    /// Categorical slot 3 (aqua).
    static let series3 = dynamic(light: 0x1BAF7A, dark: 0x199E70)
    /// Categorical slot 4 (yellow).
    static let series4 = dynamic(light: 0xEDA100, dark: 0xC98500)

    // MARK: Spacing — a 4pt scale, so vertical rhythm is decided once

    static let gutter: CGFloat = 16
    static let sectionGap: CGFloat = 16
    static let rowGap: CGFloat = 10
    static let tightGap: CGFloat = 4
    static let popoverWidth: CGFloat = 380

    /// The popover must never be taller than the screen it hangs from. AppKit does
    /// not scroll an oversized popover, it clips one — and it clips the *top*, so
    /// the range picker and the headline spend figure vanish while the footer stays
    /// put. That reads as a broken layout rather than as "there is more below".
    ///
    /// This is a ceiling, not a target: the popover is sized by its content and
    /// only scrolls once the content genuinely cannot fit. An earlier version
    /// capped it at a guessed 680pt, which on a 1050pt-usable display threw away
    /// 370pt and made it scroll when there was ample room — so the number comes
    /// from the screen, and nothing else.
    ///
    /// `visibleFrame` already excludes the menu bar the popover hangs from; the
    /// margin is for its arrow and a little breathing room at the bottom. The floor
    /// keeps it sane on a very small or unusually configured display.
    /// - Parameter screen: the display the popover will appear on. Pass the one the
    ///   status item is on, not `NSScreen.main` — with an external display attached
    ///   those differ, and sizing to the wrong one is how a popover ends up taller
    ///   than the screen it is drawn on.
    static func popoverMaxHeight(on screen: NSScreen?) -> CGFloat {
        let visible = (screen ?? NSScreen.main)?.visibleFrame.height ?? 800
        // The floor keeps it usable on a very short display rather than collapsing
        // to nothing; the ScrollView makes that case navigable.
        return max(360, visible - 32)
    }

    /// Convenience for contexts with no status item to ask, such as the CI check.
    static var popoverMaxHeight: CGFloat { popoverMaxHeight(on: NSScreen.main) }

    // MARK: Type scale
    //
    // Set once here rather than sprinkled through the views, so the popover has
    // one rhythm. Sizes sit at or above macOS conventions — a meter is glanced
    // at from a distance, not read like a document.

    static let hero = Font.system(size: 34, weight: .semibold, design: .rounded)
    static let title = Font.system(size: 15, weight: .semibold)
    static let body = Font.system(size: 13)
    static let bodyStrong = Font.system(size: 13, weight: .semibold)
    static let label = Font.system(size: 12, weight: .medium)
    static let caption = Font.system(size: 11)
    static let micro = Font.system(size: 10)
    static let mono = Font.system(size: 12, weight: .medium, design: .monospaced)

    // MARK: Roles

    /// Colour per provider.
    ///
    /// Fixed by vendor rather than by slot order, so a provider keeps its colour
    /// no matter how rows sort or which providers happen to be present. Claude
    /// takes orange because that is what people already associate with it; the
    /// hues are the validated categorical steps, not arbitrary brand grabs.
    static func providerColor(_ provider: String) -> Color {
        switch provider {
        case "claude", "claude_code", "anthropic": return series2   // orange
        case "codex", "openai": return series3                      // aqua
        case "gemini", "google": return series1                     // blue
        case "deepseek", "mistral", "xai": return series4           // yellow
        default: return Color.secondary
        }
    }

    /// Human-facing provider name.
    static func providerLabel(_ provider: String) -> String {
        switch provider {
        case "claude_code": return "Claude Code"
        case "claude": return "Claude"
        case "codex": return "Codex"
        default: return provider.capitalized
        }
    }

    /// Quota state. Returns the colour *and* a label, because colour alone is
    /// never allowed to carry the meaning.
    static func quotaState(_ percent: Double) -> (color: Color, label: String) {
        switch percent {
        case ..<70: return (good, "healthy")
        case ..<90: return (warning, "getting full")
        default: return (critical, "nearly exhausted")
        }
    }

    // MARK: Helpers

    /// A colour that resolves per appearance, so dark mode is a selected step
    /// rather than an automatic flip of the light one.
    static func dynamic(light: UInt32, dark: UInt32) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            return NSColor(Color(hex: isDark ? dark : light))
        })
    }
}

extension Color {
    init(hex: UInt32) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255,
            opacity: 1
        )
    }
}

// MARK: - Reusable pieces

/// A grouped block.
///
/// Secondary sections sit on a faint raised surface so the eye can find them
/// without a rule between every row. The hero and its chart stay on the base
/// surface — carding everything would flatten the hierarchy it is meant to
/// create.
struct Card<Content: View>: View {
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            content
        }
        .padding(.horizontal, 11)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(Color.primary.opacity(0.045))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.06), lineWidth: 1)
        )
    }
}

/// A small caps section label. Cheap structure — it lets the eye find sections
/// without drawing a rule between every block.
struct SectionLabel: View {
    let text: String
    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 10, weight: .semibold))
            .tracking(0.7)
            .foregroundStyle(.secondary)
    }
}

/// The chart's range selector.
///
/// A real control rather than three buttons: the ranges are mutually exclusive
/// views of the same measure, which is exactly what a segmented picker says.
struct RangePicker: View {
    let ranges: [ChartRange]
    @Binding var selection: String

    var body: some View {
        Picker("", selection: $selection) {
            ForEach(ranges) { range in
                Text(range.label).tag(range.key)
            }
        }
        .pickerStyle(.segmented)
        .labelsHidden()
        .controlSize(.small)
    }
}

/// Legend for the stacked chart. Present whenever more than one provider
/// appears, so identity is never carried by colour alone.
struct ProviderLegend: View {
    let providers: [String]

    var body: some View {
        HStack(spacing: 10) {
            ForEach(providers, id: \.self) { provider in
                HStack(spacing: 4) {
                    RoundedRectangle(cornerRadius: 1.5)
                        .fill(Theme.providerColor(provider))
                        .frame(width: 7, height: 7)
                    Text(Theme.providerLabel(provider))
                        .font(Theme.micro).foregroundStyle(.secondary)
                }
            }
        }
    }
}

/// A pill stating how much to trust the number beside it.
///
/// `exact` means the provider reported it; `est` means we derived it. This is
/// the honesty argument made visible, so it is a real component rather than a
/// grey caption.
struct TrustBadge: View {
    let isExact: Bool

    var body: some View {
        HStack(spacing: 3) {
            Image(systemName: isExact ? "checkmark.seal.fill" : "chart.line.uptrend.xyaxis")
                .font(.system(size: 8, weight: .bold))
            Text(isExact ? "exact" : "est")
                .font(.system(size: 10, weight: .semibold))
        }
        .padding(.horizontal, 6)
        .padding(.vertical, 2)
        .background((isExact ? Theme.good : Theme.warning).opacity(0.16), in: Capsule())
        .foregroundStyle(isExact ? Theme.good : Theme.serious)
    }
}

/// Magnitude bar. Square at the baseline, rounded at the data end, per the
/// mark spec — the rounding marks where the value stops.
struct ShareBar: View {
    let fraction: Double
    var tint: Color = Theme.magnitude
    var height: CGFloat = 4

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Theme.magnitudeTrack.opacity(0.45))
                Capsule()
                    .fill(tint)
                    .frame(width: max(geo.size.width * min(max(fraction, 0), 1), 2))
            }
        }
        .frame(height: height)
    }
}

/// Axis scale: a rounded ceiling and the ticks to label.
///
/// Ticks are rounded to clean numbers rather than to the raw peak, because an
/// axis labelled "$40.54" makes the reader do arithmetic to place a bar. The
/// ceiling snaps to 1 / 2 / 2.5 / 5 x a power of ten.
struct AxisScale {
    let max: Double
    let ticks: [Double]

    init(peak: Double, divisions: Int = 2) {
        guard peak > 0 else {
            self.max = 1
            self.ticks = [0, 1]
            return
        }
        let magnitude = pow(10, floor(log10(peak)))
        let normalised = peak / magnitude
        // Finer than the usual 1/2/5 ladder: with only two divisions, a coarse
        // ceiling wastes most of the plot. A $12 peak rounding to $20 left the
        // tallest column at 60% height with nothing above it.
        let step: Double
        switch normalised {
        case ..<1.0: step = 1.0
        case ..<1.5: step = 1.5
        case ..<2.0: step = 2.0
        case ..<2.5: step = 2.5
        case ..<3.0: step = 3.0
        case ..<4.0: step = 4.0
        case ..<5.0: step = 5.0
        case ..<6.0: step = 6.0
        case ..<8.0: step = 8.0
        default: step = 10.0
        }
        let ceiling = step * magnitude
        self.max = ceiling
        self.ticks = (0...divisions).map { ceiling * Double($0) / Double(divisions) }
    }

    /// Compact money for an axis label. Sub-dollar values keep cents because
    /// rounding them all to "$0" would flatten the scale into nonsense.
    static func label(_ value: Double) -> String {
        if value == 0 { return "$0" }
        if value >= 1000 { return String(format: "$%.0fK", value / 1000) }
        if value >= 10 { return String(format: "$%.0f", value) }
        if value >= 1 { return String(format: "$%.1f", value) }
        return String(format: "$%.2f", value)
    }
}

/// Cost per bucket, stacked by provider, with a value axis and hover readout.
///
/// Columns rather than a line: each bucket is a discrete magnitude, not a
/// reading along a continuum. Segments are stacked because the parts sum to a
/// meaningful whole — what a period cost, and who it went to.
///
/// Tick labels are *positioned* against their gridlines rather than distributed
/// with spacers. Spacers align the top of a text box, not the middle of the
/// glyphs, which left every label sitting half a line below its line.
struct UsageChart: View {
    let points: [ChartPoint]
    let providers: [String]
    var plotHeight: CGFloat = 68

    /// Wide enough for "$100" without crowding the plot.
    private let gutter: CGFloat = 34
    private let gap: CGFloat = 10
    private let tickTextHeight: CGFloat = 12

    @State private var hovered: Int?

    private var scale: AxisScale { AxisScale(peak: points.map(\.total).max() ?? 0) }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            readout
            // Labels sit to the RIGHT of the plot so the columns start at the
            // container's leading edge, in line with every other row in the
            // popover. A left gutter indented the whole chart against the text
            // above and below it.
            HStack(alignment: .top, spacing: gap) {
                plot
                axisLabels
            }
            xAxis
        }
    }

    // MARK: Hover readout
    //
    // A bar chart with no way to read a value forces the reader to estimate off
    // the gridlines. A single readout above the plot answers it exactly, without
    // the chaos of a number printed on every column.

    private var readout: some View {
        HStack(spacing: 6) {
            if let index = hovered, points.indices.contains(index) {
                let point = points[index]
                Text(UsageChart.tickLabel(point.label))
                    .font(.system(size: 10, weight: .semibold))
                Text(AxisScale.label(point.total))
                    .font(.system(size: 10, weight: .semibold)).monospacedDigit()
                if providers.count > 1 {
                    ForEach(providers.filter { (point.byProvider[$0] ?? 0) > 0 }, id: \.self) { p in
                        HStack(spacing: 3) {
                            Circle().fill(Theme.providerColor(p)).frame(width: 5, height: 5)
                            Text(AxisScale.label(point.byProvider[p] ?? 0))
                                .font(.system(size: 9)).monospacedDigit()
                        }
                    }
                }
                Spacer(minLength: 0)
            } else {
                // Deliberately blank. The row keeps its height so the chart does
                // not jump when a value appears, but an idle prompt would be
                // permanent clutter for a one-time discovery.
                Spacer(minLength: 0)
            }
        }
        .frame(height: 13)          // reserved, so the chart never jumps on hover
        .foregroundStyle(.secondary)
    }

    // MARK: Axis

    private var axisLabels: some View {
        ZStack(alignment: .topLeading) {
            ForEach(scale.ticks, id: \.self) { tick in
                Text(AxisScale.label(tick))
                    .font(.system(size: 9)).monospacedDigit()
                    .foregroundStyle(.tertiary)
                    .frame(height: tickTextHeight, alignment: .leading)
                    // Centre the glyphs on the line this label belongs to.
                    .offset(y: yPosition(for: tick) - tickTextHeight / 2)
            }
        }
        .frame(width: gutter, height: plotHeight, alignment: .topLeading)
    }

    private func yPosition(for tick: Double) -> CGFloat {
        plotHeight * CGFloat(1 - tick / scale.max)
    }

    private var plot: some View {
        GeometryReader { geo in
            ZStack(alignment: .topLeading) {
                // Solid hairlines, one step off the surface. Never dashed —
                // dashing reads as a projection or a threshold.
                ForEach(scale.ticks, id: \.self) { tick in
                    Rectangle()
                        .fill(Color.secondary.opacity(tick == 0 ? 0.30 : 0.12))
                        .frame(height: 1)
                        .offset(y: yPosition(for: tick))
                }
                columns(size: geo.size)
            }
            .contentShape(Rectangle())
            .onContinuousHover { phase in
                switch phase {
                case .active(let location):
                    // The hit target is the whole slot, not just the bar, so a
                    // one-pixel column is still reachable.
                    let slot = geo.size.width / CGFloat(max(points.count, 1))
                    let index = Int(location.x / max(slot, 1))
                    hovered = points.indices.contains(index) ? index : nil
                case .ended:
                    hovered = nil
                }
            }
        }
        .frame(height: plotHeight)
    }

    // MARK: Columns

    private func columns(size: CGSize) -> some View {
        let count = max(points.count, 1)
        let slot = size.width / CGFloat(count)
        let barWidth = max(min(slot - 2, 22), 2)

        return HStack(alignment: .bottom, spacing: 2) {
            ForEach(Array(points.enumerated()), id: \.offset) { index, point in
                column(point, index: index, width: barWidth, available: size.height)
            }
        }
        .frame(width: size.width, height: size.height, alignment: .bottomLeading)
    }

    private func column(_ point: ChartPoint, index: Int,
                        width: CGFloat, available: CGFloat) -> some View {
        // An empty bucket keeps its slot, drawn as a hairline. Dropping it would
        // compress idle periods out of existence and make a burst look like
        // steady work.
        let total = point.total
        let fullHeight = total > 0 ? max(available * CGFloat(total / scale.max), 3) : 1.5
        let isHovered = hovered == index
        let isLatest = index == points.count - 1
        let emphasis: Double = isHovered ? 1 : (isLatest ? 0.95 : 0.68)

        return VStack(spacing: 2) {   // 2px surface gap between stacked fills
            if total > 0 {
                ForEach(providers.filter { (point.byProvider[$0] ?? 0) > 0 }, id: \.self) { provider in
                    let share = (point.byProvider[provider] ?? 0) / total
                    Rectangle()
                        .fill(Theme.providerColor(provider).opacity(emphasis))
                        .frame(width: width, height: max(fullHeight * CGFloat(share), 1.5))
                }
            } else {
                // Idle buckets must be perceptible — showing that nothing
                // happened is the whole reason they keep their slot. At 0.16
                // they vanished against a dark surface.
                Rectangle()
                    .fill(Color.secondary.opacity(isHovered ? 0.55 : 0.30))
                    .frame(width: width, height: max(fullHeight, 2))
            }
        }
        // Rounded at the data end, square at the baseline: the rounding marks
        // where the value stops.
        .clipShape(UnevenRoundedRectangle(
            topLeadingRadius: 3, bottomLeadingRadius: 0,
            bottomTrailingRadius: 0, topTrailingRadius: 3
        ))
        .frame(height: available, alignment: .bottom)
    }

    /// First and last bucket only. A label under every column is chaos and goes
    /// unread; the ends place the range and the hover readout does the rest.
    private var xAxis: some View {
        HStack(spacing: 0) {
            if let first = points.first, let last = points.last {
                Text(UsageChart.tickLabel(first.label))
                Spacer(minLength: 0)
                Text(UsageChart.tickLabel(last.label))
            }
            // Matches the value-label column on the right, so the last bucket's
            // label stays over the last column rather than over the axis.
            Spacer().frame(width: gutter + gap)
        }
        .font(.system(size: 9)).monospacedDigit().foregroundStyle(.tertiary)
    }

    /// `2026-08-21T13` -> `13:00`; `2026-08-21` -> `Aug 21`.
    static func tickLabel(_ raw: String) -> String {
        if raw.count == 13, let hour = raw.split(separator: "T").last {
            return "\(hour):00"
        }
        let parts = raw.split(separator: "-")
        guard parts.count == 3, let month = Int(parts[1]) else { return raw }
        let names = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        let name = (1...12).contains(month) ? names[month] : String(parts[1])
        return "\(name) \(parts[2])"
    }
}
