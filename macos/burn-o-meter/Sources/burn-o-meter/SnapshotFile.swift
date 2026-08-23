import Foundation

/// Reads the payload the Python engine writes to `~/.burn-o-meter/snapshot.json`.
///
/// The app deliberately does **not** query SQLite. Two reasons:
///
/// * macOS's system SQLite refuses to open a WAL-mode database on a read-only
///   connection — it cannot create the `-shm` — and the alternative, opening it
///   read-write, would throw away the guarantee that a UI bug cannot corrupt the
///   store.
/// * Every aggregation rule (bases never fuse, unpriced is not zero, effective
///   rate, window grouping) already exists in Python and is covered by tests.
///   Re-implementing it in Swift would create a second source of truth that can
///   silently drift from what `burnometer` prints.
///
/// So the menu bar renders numbers; it never computes them.
enum SnapshotFile {

    static func defaultPath() -> String {
        if let override = ProcessInfo.processInfo.environment["BURNOMETER_HOME"] {
            return (override as NSString).appendingPathComponent("snapshot.json")
        }
        return NSHomeDirectory() + "/.burn-o-meter/snapshot.json"
    }

    /// Newer schemas may add fields; a *lower* major schema means this app is
    /// newer than the engine that wrote the file and cannot trust its shape.
    static let supportedSchema = 2

    static func load(path: String = defaultPath()) -> Snapshot {
        guard FileManager.default.fileExists(atPath: path) else {
            return Snapshot(error: "No data yet. Run `burn-o-meter scan`, or "
                            + "`burn-o-meter agent install` to keep it current.")
        }
        do {
            let data = try Data(contentsOf: URL(fileURLWithPath: path))
            guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                return Snapshot(error: "Could not read \(path)")
            }
            let schema = root["schema"] as? Int ?? 0
            guard schema >= supportedSchema else {
                return Snapshot(error: "Snapshot is from an older engine (schema \(schema)). "
                                + "Run `burn-o-meter scan` to rewrite it.")
            }
            return parse(root)
        } catch {
            return Snapshot(error: error.localizedDescription)
        }
    }

    // MARK: - Parsing

    private static func parse(_ root: [String: Any]) -> Snapshot {
        var snapshot = Snapshot()
        snapshot.generatedAt = (root["generated_at"] as? String).flatMap(parseDate)

        if let today = root["today"] as? [String: Any] {
            snapshot.todayRows = (today["rows"] as? [[String: Any]] ?? []).map(parseRow)
            snapshot.subtotalsByBasis = (today["subtotals"] as? [String: Any] ?? [:])
                .compactMapValues { ($0 as? [String: Any])?["cost_usd"] as? Double }
            snapshot.basisNotes = (today["cost_basis_notes"] as? [String: String]) ?? [:]
            snapshot.unpricedModels = (today["unpriced_models"] as? [String]) ?? []
        }

        snapshot.dailyCosts = (root["sparkline"] as? [[String: Any]] ?? []).map {
            DayCost(day: $0["day"] as? String ?? "", costUSD: $0["cost_usd"] as? Double ?? 0)
        }

        snapshot.ranges = (root["ranges"] as? [[String: Any]] ?? []).map { raw in
            ChartRange(
                key: raw["key"] as? String ?? "",
                label: raw["label"] as? String ?? "",
                bucket: raw["bucket"] as? String ?? "day",
                providers: raw["providers"] as? [String] ?? [],
                points: (raw["points"] as? [[String: Any]] ?? []).map { point in
                    ChartPoint(
                        label: point["label"] as? String ?? "",
                        byProvider: (point["by_provider"] as? [String: Any] ?? [:])
                            .compactMapValues { $0 as? Double },
                        total: point["total"] as? Double ?? 0
                    )
                },
                subtotalsByBasis: (raw["subtotals"] as? [String: Any] ?? [:])
                    .compactMapValues { ($0 as? [String: Any])?["cost_usd"] as? Double },
                cache: {
                    let c = raw["cache"] as? [String: Any] ?? [:]
                    return CacheStats(
                        hitRate: c["hit_rate"] as? Double,
                        cacheRead: c["cache_read"] as? Int ?? 0,
                        freshInput: c["fresh_input"] as? Int ?? 0,
                        cacheWrite: c["cache_write"] as? Int ?? 0,
                        savingsUSD: c["savings_usd"] as? Double,
                        actualUSD: c["actual_usd"] as? Double,
                        withoutCacheUSD: c["without_cache_usd"] as? Double
                    )
                }()
            )
        }

        snapshot.quotas = (root["quotas"] as? [[String: Any]] ?? []).map {
            Quota(
                provider: $0["provider"] as? String ?? "",
                window: $0["window"] as? String ?? "",
                usedPercent: $0["used_percent"] as? Double,
                windowMinutes: $0["window_minutes"] as? Int ?? 0,
                resetsAt: ($0["resets_at"] as? String).flatMap(parseDate),
                planType: $0["plan_type"] as? String,
                isExact: $0["exact"] as? Bool ?? false,
                ageSeconds: $0["age_seconds"] as? Int
            )
        }
        .sorted { $0.priority < $1.priority }

        if let window = root["current_window"] as? [String: Any] {
            snapshot.currentWindow = UsageWindow(
                requests: window["requests"] as? Int ?? 0,
                costUSD: window["cost_usd"] as? Double,
                basis: CostBasis(rawValue: window["cost_basis"] as? String ?? "") ?? .unpriced,
                remainingSeconds: window["remaining_seconds"] as? Int ?? 0,
                relativeToMedian: window["relative_to_median"] as? Double,
                hasPublishedLimit: window["has_published_limit"] as? Bool ?? false
            )
        }
        return snapshot
    }

    private static func parseRow(_ raw: [String: Any]) -> ModelRow {
        ModelRow(
            model: raw["model"] as? String ?? "unknown",
            provider: raw["provider"] as? String ?? "",
            basis: CostBasis(rawValue: raw["cost_basis"] as? String ?? "") ?? .unpriced,
            requests: raw["requests"] as? Int ?? 0,
            costUSD: raw["cost_usd"] as? Double,
            totalTokens: raw["total_tokens"] as? Int ?? 0,
            cacheHitRate: raw["cache_hit_rate"] as? Double,
            effectiveRate: raw["effective_rate_usd_per_mtok"] as? Double,
            priceSource: raw["price_source"] as? String,
            projects: (raw["projects"] as? [[String: Any]] ?? []).map { p in
                ProjectSlice(
                    project: p["project"] as? String ?? "—",
                    requests: p["requests"] as? Int ?? 0,
                    costUSD: p["cost_usd"] as? Double,
                    shareOfModel: p["share_of_model"] as? Double,
                    cacheHitRate: p["cache_hit_rate"] as? Double
                )
            }
        )
    }

    private static let withFraction: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    private static let plain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    static func parseDate(_ raw: String) -> Date? {
        withFraction.date(from: raw) ?? plain.date(from: raw)
    }
}
