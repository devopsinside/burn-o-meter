import AppKit
import SwiftUI

/// The status item's title as an attributed string.
///
/// A pure function of its inputs so it can be checked directly: `--check-freshness`
/// builds titles at 89% and 90% and asserts on the result, rather than inferring
/// the behaviour from a running app.
enum MenuBarTitle {
    /// - Parameters:
    ///   - text: the title as `Snapshot.menuBarTitle` renders it.
    ///   - percent: the rate-limit reading that title shows, if any.
    ///   - warn: whether the user wants the nearly-exhausted colour at all.
    static func attributed(_ text: String, percent: Double?, warn: Bool) -> NSAttributedString {
        let title = NSMutableAttributedString(
            string: text,
            attributes: [
                .font: NSFont.menuBarFont(ofSize: 0),
                // Digits and a percent sign have no descender, so they ride high
                // in the font box AppKit centres; this drops them onto the glyph's
                // centre line. See MenuBarIcon.titleBaselineOffset.
                .baselineOffset: MenuBarIcon.titleBaselineOffset,
            ]
        )
        guard let range = warningRange(in: text, percent: percent, warn: warn) else { return title }
        // Only the percentage, not the spend beside it: the limit is what is about
        // to stop work. The colour is the popover's own for this level, taken from
        // the same function, so opening the popover never shows a different one.
        let color = NSColor(Theme.quotaState(percent ?? 0).color)
        title.addAttribute(.foregroundColor, value: color, range: range)
        return title
    }

    /// The span of `text` to colour, or nil when nothing should be.
    ///
    /// Everything else in the title is left to the button, which tints it for the
    /// bar it sits on and inverts it while the item is held down. An explicit colour
    /// opts out of that, which is why it is applied to this one span and only while
    /// it means something.
    static func warningRange(in text: String, percent: Double?, warn: Bool) -> NSRange? {
        guard warn, let percent, Theme.isNearlyExhausted(percent) else { return nil }
        // The spend never contains "%", so the only match is the reading itself.
        let range = (text as NSString).range(of: percentText(percent))
        return range.location == NSNotFound ? nil : range
    }

    /// How a reading appears in the title. Shared with `Snapshot.menuBarTitle`,
    /// because `warningRange` finds the reading by searching for exactly this text.
    static func percentText(_ percent: Double) -> String {
        "\(Int(percent.rounded()))%"
    }
}
