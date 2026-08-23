// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "burn-o-meter",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            // Hyphenated so the built binary is `burn-o-meter`, matching the
            // project everywhere a user can see it. Swift maps this to the
            // module `burn_o_meter` internally, which nothing references.
            name: "burn-o-meter",
            path: "Sources/burn-o-meter",
            // SQLite ships with macOS. No third-party dependencies at all, which
            // keeps the app's supply chain as small as the engine's (G7).
            linkerSettings: [.linkedLibrary("sqlite3")]
        )
    ]
)
