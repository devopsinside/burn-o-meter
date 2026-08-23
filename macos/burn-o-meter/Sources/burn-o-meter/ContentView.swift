import SwiftUI

/// The popover.
///
/// Layout follows the data's jobs rather than a template: spend for the selected
/// range is a single headline, so it is a hero number and not a chart; cost over
/// time is change-over-time split by identity, so it is a stacked column chart;
/// rate limits are state, so they take a reserved status colour plus a number
/// and a word.
///
/// Order is deliberate. On a subscription the rolling window is the binding
/// constraint — "can I keep going" is what interrupts work — so limits sit above
/// spend. On an API key there is no window to exhaust and cost leads instead.
///
/// Two details are load-bearing rather than decorative: the `exact` / `est`
/// badges, which separate a number the provider reported from one we derived,
/// and the `~` on every subscription figure, which keeps the app from claiming
/// money was spent that never was.
struct ContentView: View {
    @ObservedObject var model: SnapshotModel
    var onQuit: () -> Void
    var onRefresh: () -> Void
    var onOptions: () -> Void
    var onCheckUpdates: () -> Void = {}

    @State private var selectedRange = "today"
    @State private var expandedModel: String?
    @State private var showingCostInfo = false

    private var snapshot: Snapshot { model.snapshot }

    private var range: ChartRange? {
        snapshot.ranges.first { $0.key == selectedRange } ?? snapshot.ranges.first
    }

    var body: some View {
        ScrollView(.vertical) {
            content
                .padding(Theme.gutter)
                .frame(width: Theme.popoverWidth, alignment: .topLeading)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(width: Theme.popoverWidth)
        .frame(maxHeight: Theme.popoverMaxHeight)
        // No rubber-banding when everything already fits, which is the common case.
        .scrollBounceBehavior(.basedOnSize)
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let error = snapshot.error {
                errorView(error)
            } else {
                if snapshot.isStale && !model.isRefreshing { staleBanner }
                rangeSection
                if !snapshot.quotas.isEmpty { Card { quotaSection } }
                if let range { Card { cacheSection(range.cache) } }
                if let window = snapshot.currentWindow { Card { windowSection(window) } }
                if !snapshot.todayRows.isEmpty { Card { modelSection } }
            }
            footer
        }
    }

    // MARK: - Range, hero and chart

    private var rangeSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            if snapshot.ranges.count > 1 {
                RangePicker(ranges: snapshot.ranges, selection: $selectedRange)
            }
            hero
            if let range, !range.points.isEmpty {
                VStack(alignment: .leading, spacing: 5) {
                    UsageChart(points: range.points, providers: range.providers)
                    HStack {
                        // The axis now carries the magnitudes, so the caption
                        // only has to say what a column is.
                        if range.providers.count > 1 {
                            ProviderLegend(providers: range.providers)
                        } else {
                            Text(range.bucketLabel)
                                .font(Theme.micro).foregroundStyle(.tertiary)
                        }
                        Spacer()
                    }
                }
            }
        }
    }

    private var hero: some View {
        VStack(alignment: .leading, spacing: Theme.tightGap) {
            if let subtotals = range?.subtotals, let (basis, total) = subtotals.first {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(basis.prefix + Format.money(total))
                        .font(Theme.hero).monospacedDigit()
                    if basis == .apiEquivalent {
                        Text("API-equivalent")
                            .font(Theme.micro).fontWeight(.medium)
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .background(Color.secondary.opacity(0.14), in: Capsule())
                            .foregroundStyle(.secondary)
                    }
                    // The chip labels the number; this explains it. Both are
                    // needed — the chip alone is jargon, and an explanation
                    // nobody opens is not an explanation.
                    Button { showingCostInfo = true } label: {
                        Image(systemName: "info.circle")
                            .font(.system(size: 12))
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.tertiary)
                    .help("What this number is")
                    .popover(isPresented: $showingCostInfo, arrowEdge: .bottom) {
                        costExplainer(basis: basis, total: total)
                    }
                }
                Text(snapshot.note(for: basis))
                    .font(Theme.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                // Two bases never fuse into one number, so when both are present
                // each keeps its own line and its own label.
                ForEach(subtotals.dropFirst(), id: \.0) { basis, total in
                    Text("\(basis.prefix)\(Format.money(total)) · \(snapshot.note(for: basis))")
                        .font(Theme.caption).foregroundStyle(.secondary)
                }
            } else {
                Text("—").font(Theme.hero).foregroundStyle(.tertiary)
                Text("No usage in this period")
                    .font(Theme.caption).foregroundStyle(.secondary)
            }
        }
    }

    /// What the headline number means.
    ///
    /// "API-equivalent" is jargon, and the distinction it draws — that no money
    /// changed hands — is the single thing most likely to be misread. The
    /// counterfactual does the explaining: seeing what the same work would have
    /// cost uncached makes both the label and the cache hit rate concrete.
    private func costExplainer(basis: CostBasis, total: Double) -> some View {
        // Split into named sub-views rather than one expression. SwiftUI's type
        // checker gave up on the combined version — it built locally and failed
        // on CI, which is a difference in compiler budget, not in correctness.
        VStack(alignment: .leading, spacing: 10) {
            explainerHeading(basis)
            explainerBody(basis: basis, total: total)
            explainerCache(basis: basis, total: total)
            Divider()
            explainerProvenance
        }
        .padding(14)
        .frame(width: 300)
    }

    private func explainerHeading(_ basis: CostBasis) -> some View {
        let title = basis == .apiEquivalent ? "You were not charged this"
                                            : "What you were charged"
        return Text(title).font(Theme.title)
    }

    private func explainerBody(basis: CostBasis, total: Double) -> some View {
        let text: String
        if basis == .apiEquivalent {
            text = "You are on a subscription, so this period cost you your plan fee — not "
                + Format.money(total)
                + ". This is what the same tokens would have cost at published API rates, "
                + "shown so you can compare plans and see which work is expensive."
        } else {
            text = "You pay per token against an API key, so this is real spend at "
                + "published rates."
        }
        return Text(text)
            .font(Theme.caption)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    /// The counterfactual: what the same work would have cost with no caching.
    /// Shown only when every model in the period is priced — a partial figure
    /// would understate the saving and still read as fact.
    @ViewBuilder
    private func explainerCache(basis: CostBasis, total: Double) -> some View {
        if let cache = range?.cache,
           let without = cache.withoutCacheUSD,
           let saved = cache.savingsUSD,
           let fraction = cache.savedFraction,
           without > 0 {
            Divider()
            Text("Prompt caching did most of the work").font(Theme.label)
            VStack(alignment: .leading, spacing: 4) {
                explainerRow("Same work, no caching", Format.money(without), .secondary)
                explainerRow("What it came to",
                             basis.prefix + Format.money(cache.actualUSD ?? total), .primary)
                explainerRow("Caching saved", savedLabel(saved, fraction), Theme.good)
            }
            Text("Long agent sessions re-read their whole context every turn, so almost all "
                 + "of the tokens are cache reads at a tenth of the input rate. That is why "
                 + "list prices say so little about real cost.")
                .font(Theme.micro)
                .foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func savedLabel(_ saved: Double, _ fraction: Double) -> String {
        Format.money(saved) + "  (" + String(Int(fraction * 100)) + "%)"
    }

    private var explainerProvenance: some View {
        Text("Rates come from models.dev plus our own correction for Anthropic's 1-hour "
             + "cache-write rate, which no public pricing database records. A model with "
             + "no published rate shows \u{201C}\u{2014}\u{201D}, never $0.00.")
            .font(Theme.micro)
            .foregroundStyle(.tertiary)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func explainerRow(_ label: String, _ value: String, _ tint: Color) -> some View {
        HStack {
            Text(label).font(Theme.caption).foregroundStyle(.secondary)
            Spacer()
            Text(value).font(Theme.caption).fontWeight(.semibold)
                .monospacedDigit().foregroundStyle(tint)
        }
    }

    // MARK: - Rate limits

    /// Limits, grouped by provider.
    ///
    /// Grouping matters here: a provider's windows are one budget seen at two
    /// scales, so interleaving Claude's five-hour with Codex's weekly forces the
    /// reader to re-orient on every row.
    private var quotaSection: some View {
        VStack(alignment: .leading, spacing: 11) {
            SectionLabel(text: "Rate limits")
            ForEach(quotaGroups, id: \.provider) { group in
                VStack(alignment: .leading, spacing: 7) {
                    HStack(spacing: 6) {
                        Circle()
                            .fill(Theme.providerColor(group.provider))
                            .frame(width: 7, height: 7)
                        Text(Theme.providerLabel(group.provider))
                            .font(Theme.label)
                        Spacer()
                    }
                    ForEach(group.quotas) { quota in
                        quotaRow(quota)
                    }
                    // Explain the lag once per provider, not once per row.
                    if group.quotas.contains(where: { $0.isPeriodicallySampled
                                                      && $0.freshness != .current }) {
                        Text("Claude records these about every 15 minutes, so this can "
                             + "sit behind what the Claude app shows live.")
                            .font(Theme.micro).foregroundStyle(.tertiary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    /// Providers in a stable order, each with its own windows shortest-first.
    private var quotaGroups: [(provider: String, quotas: [Quota])] {
        var order: [String] = []
        var grouped: [String: [Quota]] = [:]
        for quota in snapshot.quotas.sorted(by: { $0.priority < $1.priority }) {
            if grouped[quota.provider] == nil { order.append(quota.provider) }
            grouped[quota.provider, default: []].append(quota)
        }
        return order.map { ($0, (grouped[$0] ?? []).sorted { $0.windowMinutes < $1.windowMinutes }) }
    }

    /// Cache efficiency.
    ///
    /// The biggest lever on cost in agentic use, and invisible in a spend total:
    /// a 96% hit rate is the difference between Opus 5 costing its $5/Mtok list
    /// rate and costing about $1 all-in. Shown with what it saved, because a
    /// percentage alone does not tell anyone whether to care.
    private func cacheSection(_ cache: CacheStats) -> some View {
        let state = cache.state
        return VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                SectionLabel(text: "Cache efficiency")
                Spacer()
                Text(Format.percent(cache.hitRate))
                    .font(Theme.title).monospacedDigit()
                    .foregroundStyle(state.color)
            }
            ShareBar(fraction: cache.hitRate ?? 0, tint: state.color, height: 6)
            HStack(spacing: 6) {
                Text(state.label).foregroundStyle(state.color)
                Spacer()
                if let saved = cache.savingsUSD, saved > 0 {
                    Text("saved ~\(Format.money(saved)) vs uncached")
                } else if cache.savingsUSD == nil && cache.cacheRead > 0 {
                    // A partial figure would understate the saving and read as
                    // fact, so it is withheld rather than approximated.
                    Text("saving not computable — unpriced models")
                        .foregroundStyle(Theme.serious)
                }
            }
            .font(Theme.micro).foregroundStyle(.secondary).monospacedDigit()
        }
    }

    private func quotaRow(_ quota: Quota) -> some View {
        let used = quota.usedPercent ?? 0
        let state = Theme.quotaState(used)
        let dimmed = quota.freshness == .stale

        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(quota.windowTitle).font(Theme.caption).foregroundStyle(.secondary)
                TrustBadge(isExact: quota.isExact)
                Spacer()
                Text(String(format: "%.0f%%", used))
                    .font(Theme.title).monospacedDigit()
                    .foregroundStyle(dimmed ? AnyShapeStyle(.secondary) : AnyShapeStyle(state.color))
            }
            ShareBar(
                fraction: used / 100,
                tint: dimmed ? Color.secondary.opacity(0.5) : state.color,
                height: 6
            )
            HStack(spacing: 6) {
                switch quota.freshness {
                case .stale:
                    // Say why it is greyed out rather than leaving the reader to
                    // wonder whether the number is broken.
                    Text("as of \(Format.duration(TimeInterval(quota.ageSeconds ?? 0))) ago")
                        .foregroundStyle(Theme.serious)
                case .lagging:
                    // The word carries the meaning; the colour reinforces it,
                    // because the warning step is deliberately low contrast on a
                    // light surface.
                    Text(state.label).foregroundStyle(state.color)
                    Text("· as of \(Format.duration(TimeInterval(quota.ageSeconds ?? 0))) ago")
                case .current:
                    Text(state.label).foregroundStyle(state.color)
                }
                if let plan = quota.planLabel { Text("· \(plan)") }
                Spacer()
                if let resets = quota.resetsAt {
                    // A countdown answers "can I keep going"; a date does not,
                    // unless the reset is far enough away that a date is clearer.
                    let seconds = resets.timeIntervalSinceNow
                    if seconds > 0 && seconds < 36 * 3600 {
                        Text("resets in ~\(Format.duration(seconds))")
                    } else {
                        Text("resets \(resets.formatted(.dateTime.month().day()))")
                    }
                }
            }
            .font(Theme.micro).foregroundStyle(.secondary)
        }
    }

    // MARK: - Current window

    /// What the current five-hour window has cost.
    ///
    /// Supplementary when an exact utilisation percentage exists — that already
    /// answers "how full". Without one it is the whole answer, and the
    /// comparison against the user's own history stands in for a percentage
    /// nobody publishes.
    private func windowSection(_ window: UsageWindow) -> some View {
        let hasExactPercent = snapshot.quotas.contains {
            $0.provider == "claude" && $0.window == "five_hour" && !$0.isStale
        }
        return VStack(alignment: .leading, spacing: Theme.tightGap) {
            HStack(spacing: 6) {
                SectionLabel(text: "This window")
                Spacer()
                Text("~\(Format.duration(window.remaining)) left")
                    .font(Theme.caption).fontWeight(.medium).monospacedDigit()
                    .foregroundStyle(.secondary)
            }
            HStack(spacing: 8) {
                Text("\(window.requests) requests")
                if let cost = window.costUSD {
                    Text("· \(window.basis.prefix)\(Format.money(cost))")
                }
                Spacer()
                if let ratio = window.relativeToMedian {
                    Text(String(format: "%.1f× your median", ratio))
                        .foregroundStyle(ratio >= 2 ? AnyShapeStyle(Theme.serious)
                                                    : AnyShapeStyle(.secondary))
                }
            }
            .font(Theme.caption).foregroundStyle(.secondary).monospacedDigit()

            if !hasExactPercent {
                Text("Anthropic publishes no token limit for subscription plans, so this "
                     + "compares against your own history rather than showing a percentage.")
                    .font(Theme.micro).foregroundStyle(.tertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: - Models

    private var modelSection: some View {
        VStack(alignment: .leading, spacing: Theme.rowGap) {
            SectionLabel(text: "Today by model")
            ForEach(snapshot.todayRows.prefix(4)) { row in
                modelRow(row, share: share(of: row))
            }
            if !snapshot.unpricedModels.isEmpty {
                Label(
                    "\(snapshot.unpricedModels.count) model(s) have no published rate — "
                    + "shown as “—”, never $0.00",
                    systemImage: "questionmark.circle"
                )
                .font(Theme.micro).foregroundStyle(Theme.serious)
                .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func modelRow(_ row: ModelRow, share: Double) -> some View {
        let isExpanded = expandedModel == row.model
        let canExpand = !row.projects.isEmpty

        return VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                // A disclosure arrow only where there is something to disclose;
                // an affordance that does nothing is worse than none.
                if canExpand {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 8, weight: .bold))
                        .foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(isExpanded ? 90 : 0))
                        .frame(width: 8)
                } else {
                    Spacer().frame(width: 8)
                }
                // Colour identifies the provider, not the row's rank — sorting
                // by spend must never repaint anything.
                Circle()
                    .fill(Theme.providerColor(row.provider))
                    .frame(width: 7, height: 7)
                Text(row.model).font(Theme.mono)
                    .lineLimit(1).truncationMode(.middle)
                Spacer(minLength: 6)
                Text(Format.cost(row.costUSD, row.basis))
                    .font(Theme.bodyStrong).monospacedDigit()
                    .foregroundStyle(row.costUSD == nil ? .secondary : .primary)
            }
            ShareBar(fraction: share, tint: Theme.providerColor(row.provider), height: 5)
            HStack(spacing: 10) {
                Text("\(row.requests) req")
                Text("cache \(Format.percent(row.cacheHitRate))")
                Spacer()
                if let rate = row.effectiveRate {
                    Text(String(format: "$%.2f/Mtok", rate))
                } else {
                    Text("unpriced").foregroundStyle(Theme.serious)
                }
            }
            .font(Theme.micro).foregroundStyle(.secondary).monospacedDigit()

            if isExpanded {
                projectBreakdown(row)
            }
        }
        // The whole row is the hit target, not just the chevron.
        .contentShape(Rectangle())
        .onTapGesture {
            guard canExpand else { return }
            withAnimation(.easeOut(duration: 0.12)) {
                expandedModel = isExpanded ? nil : row.model
            }
        }
    }

    /// Where a model's spend went.
    ///
    /// Shares are of the *model*, not of the day, because that is the question
    /// the reader just asked by expanding the row.
    private func projectBreakdown(_ row: ModelRow) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(row.projects) { slice in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(slice.project)
                        .font(Theme.micro).lineLimit(1).truncationMode(.middle)
                    Spacer(minLength: 6)
                    Text("\(slice.requests) req")
                        .font(Theme.micro).foregroundStyle(.tertiary).monospacedDigit()
                    Text(Format.cost(slice.costUSD, row.basis))
                        .font(Theme.micro).monospacedDigit()
                        .frame(width: 54, alignment: .trailing)
                }
                ShareBar(
                    fraction: slice.shareOfModel ?? 0,
                    tint: Theme.providerColor(row.provider).opacity(0.55),
                    height: 3
                )
            }
        }
        .padding(.leading, 15)
        .padding(.top, 3)
        .transition(.opacity.combined(with: .move(edge: .top)))
    }

    /// Share within the row's own cost basis — a subscription figure must never
    /// dilute a real charge.
    private func share(of row: ModelRow) -> Double {
        guard let cost = row.costUSD,
              let total = snapshot.subtotalsByBasis[row.basis.rawValue],
              total > 0
        else { return 0 }
        return cost / total
    }

    // MARK: - Chrome

    private var footer: some View {
        HStack(spacing: 10) {
            if model.isRefreshing {
                Text("refreshing…").font(Theme.micro).foregroundStyle(.tertiary)
            } else {
                Text("updated \(Format.ago(snapshot.generatedAt))")
                    .font(Theme.micro)
                    .foregroundStyle(snapshot.isStale
                                     ? AnyShapeStyle(Theme.serious)
                                     : AnyShapeStyle(.tertiary))
            }
            Spacer()
            // Before refresh, because it is the rarer action and reads left-to-right
            // as "is this current?" then "make it current". Nothing checks on its
            // own, so this is the only way an update is ever found.
            Button(action: onCheckUpdates) {
                Image(systemName: "arrow.down.circle").font(.system(size: 12))
            }
            .buttonStyle(.plain).foregroundStyle(.secondary)
            .help("Check for updates — you have \(Preferences.version)")

            Button(action: onRefresh) {
                Image(systemName: "arrow.clockwise").font(.system(size: 12))
            }
            .buttonStyle(.plain).foregroundStyle(.secondary).help("Scan now")

            // Right-click on the status item opens the same menu, but nothing
            // advertises that, so it needs a visible affordance too.
            Button(action: onOptions) {
                Image(systemName: "gearshape").font(.system(size: 12))
            }
            .buttonStyle(.plain).foregroundStyle(.secondary)
            .help("Options — launch at login, data folder, quit")

            Button(action: onQuit) {
                Image(systemName: "power").font(.system(size: 12))
            }
            .buttonStyle(.plain).foregroundStyle(.secondary).help("Quit burn-o-meter")
        }
        .padding(.top, 2)
    }

    private var staleBanner: some View {
        HStack(alignment: .top, spacing: 7) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 12)).foregroundStyle(Theme.serious)
            VStack(alignment: .leading, spacing: 2) {
                Text("Could not refresh — showing data from \(Format.ago(snapshot.generatedAt))")
                    .font(Theme.caption).fontWeight(.semibold)
                Text("The engine could not be run. Try `burn-o-meter scan` in a terminal.")
                    .font(Theme.micro).foregroundStyle(.secondary)
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.serious.opacity(0.12), in: RoundedRectangle(cornerRadius: 7))
    }

    private func errorView(_ message: String) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Label("No data yet", systemImage: "tray")
                .font(Theme.title)
            Text(message)
                .font(Theme.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.vertical, 8)
    }
}
