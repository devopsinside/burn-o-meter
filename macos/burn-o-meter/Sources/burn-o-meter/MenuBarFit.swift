import AppKit

/// Whether the status item can actually be seen, and what to do when it cannot.
///
/// On a display with a camera notch, macOS lays menu bar items out right to left
/// from Control Center and, when they run out of room, keeps placing them - under
/// the notch and to its left, where nothing is drawn. The item is not removed and
/// not told: its window still reports itself visible, because the notch is a hole
/// in the screen rather than a window covering it. Measured on a 13-inch MacBook:
/// an item that could not fit reported `occlusionState` visible while sitting at
/// x 679, inside a notch spanning 562…718. Its position is the only honest signal.
///
/// Items are placed on whichever display's menu bar is active, so the same item
/// can be fine on an external display and hidden the moment work moves to the
/// built-in one - and whether it fits depends on how many menus the frontmost app
/// has. That is "it keeps disappearing".
enum MenuBarFit {
    /// The right edge of the notch on `screen`, in global coordinates, or nil for
    /// a display without one.
    static func notchRightEdge(of screen: NSScreen) -> CGFloat? {
        guard let right = screen.auxiliaryTopRightArea else { return nil }
        // Screen-local, per AppKit's documentation; tolerate a global rect too.
        let x = right.minX >= screen.frame.minX ? right.minX : screen.frame.minX + right.minX
        return x
    }

    /// True when an item whose left edge is at `itemMinX` cannot be seen on a
    /// display whose notch ends at `notchRightEdge`: any part of it under the notch
    /// or beyond it is not drawn, and a clipped number is worse than none.
    static func isHidden(itemMinX: CGFloat, notchRightEdge: CGFloat?) -> Bool {
        guard let edge = notchRightEdge else { return false }
        return itemMinX < edge - 0.5
    }

    /// True when the item runs past either edge of its display. Measured on an
    /// external 1920pt display: an over-wide item was placed at x 2587 with width
    /// 1028, ending 415pt beyond the screen's right edge at 3200.
    static func isOffScreen(item: NSRect, screen: NSRect) -> Bool {
        item.minX < screen.minX - 0.5 || item.maxX > screen.maxX + 0.5
    }

    /// The live check against the real item.
    static func isHidden(_ item: NSStatusItem) -> Bool {
        guard let window = item.button?.window, let screen = window.screen else { return false }
        return isOffScreen(item: window.frame, screen: screen.frame)
            || isHidden(itemMinX: window.frame.minX, notchRightEdge: notchRightEdge(of: screen))
    }

    /// A short description of where the item is, for the log.
    static func placement(_ item: NSStatusItem) -> String {
        guard let window = item.button?.window, let screen = window.screen else { return "no window" }
        let notch = notchRightEdge(of: screen).map { String(format: " notch-edge %.0f", $0) } ?? ""
        return String(format: "x %.0f w %.0f on %.0fpt display%@",
                      window.frame.minX, window.frame.width, screen.frame.width, notch)
    }
}

/// How wide the status item's slot should be.
///
/// Every change of width makes macOS lay the menu bar out again, and on a crowded
/// one that is when things go wrong. v0.6.7 dropped to the glyph alone whenever the
/// item was hidden and restored the title on every app switch; macOS widened the
/// item to the right, over icons it had already packed beside the glyph, and then
/// pushed it back under the notch - the overlap in the report, flipping every few
/// seconds. So the width changes as rarely as the text allows: it grows at once,
/// because the text must fit, and shrinks only once a narrower title has lasted ten
/// minutes, so a percentage falling at a window reset does not reshuffle the bar.
enum MenuBarWidth {
    static let shrinkAfter: TimeInterval = 600

    /// The length to give the item for content of this width. macOS adds 8pt each
    /// side on top of whatever length is set, fixed or variable - measured: content
    /// 99.5 made a 116pt window as a variable item, and a 102pt length made a 118pt
    /// window - so the length is the content alone. Adding the padding here, as a
    /// first version did, padded every title twice.
    static func needed(contentWidth: CGFloat) -> CGFloat {
        contentWidth.rounded(.up)
    }

    /// The next slot width, and since when a narrower title has been wanted.
    static func next(
        current: CGFloat, needed: CGFloat, narrowerSince: Date?, now: Date
    ) -> (width: CGFloat, narrowerSince: Date?) {
        if needed > current + 0.5 { return (needed, nil) }
        if needed >= current - 0.5 { return (current, nil) }
        let since = narrowerSince ?? now
        if now.timeIntervalSince(since) >= shrinkAfter { return (needed, nil) }
        return (current, since)
    }
}

/// Transitions of the item's visibility, appended to `menubar.log` in the data
/// directory so the engine's `doctor` can say whether, and how often, macOS hid it.
/// Only the transitions are written, never what was on screen or which app was in
/// front, and the file is kept short.
enum MenuBarLog {
    static let maxLines = 400

    static var path: String { Preferences.dataDirectory + "/menubar.log" }

    static func record(_ event: String, _ detail: String) {
        let stamp = ISO8601DateFormatter().string(from: Date())
        let line = "\(stamp) \(event) \(detail)\n"
        let url = URL(fileURLWithPath: path)
        var lines = (try? String(contentsOf: url, encoding: .utf8))?
            .split(separator: "\n", omittingEmptySubsequences: true).map(String.init) ?? []
        lines.append(String(line.dropLast()))
        if lines.count > maxLines { lines.removeFirst(lines.count - maxLines) }
        let text = lines.joined(separator: "\n") + "\n"
        // 0600 from creation, like everything else in the data directory.
        let fm = FileManager.default
        if !fm.fileExists(atPath: path) {
            fm.createFile(atPath: path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        try? text.write(to: url, atomically: true, encoding: .utf8)
        try? fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path)
    }
}
