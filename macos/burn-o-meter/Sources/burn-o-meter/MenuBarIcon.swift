import AppKit
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers

/// The status-bar glyph: the same meter-and-flame as the app icon, reduced to a
/// single-colour silhouette.
///
/// Drawn in code rather than bundled as art, for the same reason `make-icon.swift`
/// is: a diff can show what changed about it, and it renders exactly at whatever
/// size and backing scale the bar happens to be, with no asset set to keep in step.
///
/// **It is a template image**, which is what makes it correct rather than merely
/// present. macOS re-tints a template to suit the menu bar it lands in — black on a
/// light bar, white on a dark one, inverted again while the item is clicked, and
/// adjusted under Increase Contrast and Reduce Transparency. A coloured image opts
/// out of every one of those and keeps its own colours against a highlight drawn
/// behind it. So the brand orange deliberately does not survive into the bar; it is
/// carried by the app icon and the popover, where there is a background to own it.
///
/// The app icon's own trick for separating the flame from the dial — a halo stroked
/// in the background colour — cannot work here, because a template has no colours to
/// halo with. The gap is cut instead: the flame's silhouette is cleared out of the
/// arc before the flame is filled, so the two read as separate shapes in one colour.
enum MenuBarIcon {
    /// Point height of the glyph. The bar gives ~22pt; 16 leaves the breathing room
    /// macOS itself uses, and keeps the arc from touching the menu bar's edges.
    static let height: CGFloat = 18

    /// Built once. `NSStatusItem` asks for this on every redraw.
    static let image: NSImage = make()

    private static func make() -> NSImage {
        let size = NSSize(width: height, height: height)
        let image = NSImage(size: size, flipped: false) { rect in
            guard let ctx = NSGraphicsContext.current?.cgContext else { return false }
            draw(in: ctx, box: rect)
            return true
        }
        // The whole point: let AppKit own the colour.
        image.isTemplate = true
        return image
    }

    /// A flame in a 0...1 box, the same curve the app icon uses. Kept as its own
    /// function so the two silhouettes cannot drift apart.
    static func flamePath(in rect: CGRect) -> CGPath {
        let p = CGMutablePath()
        func pt(_ x: CGFloat, _ y: CGFloat) -> CGPoint {
            CGPoint(x: rect.minX + x * rect.width, y: rect.minY + y * rect.height)
        }
        p.move(to: pt(0.50, 0.00))
        p.addCurve(to: pt(0.94, 0.40), control1: pt(0.78, 0.01), control2: pt(0.94, 0.17))
        p.addCurve(to: pt(0.63, 0.69), control1: pt(0.94, 0.57), control2: pt(0.72, 0.56))
        p.addCurve(to: pt(0.50, 1.00), control1: pt(0.58, 0.80), control2: pt(0.535, 0.90))
        p.addCurve(to: pt(0.37, 0.69), control1: pt(0.465, 0.90), control2: pt(0.42, 0.80))
        p.addCurve(to: pt(0.06, 0.40), control1: pt(0.28, 0.56), control2: pt(0.06, 0.57))
        p.addCurve(to: pt(0.50, 0.00), control1: pt(0.06, 0.17), control2: pt(0.22, 0.01))
        p.closeSubpath()
        return p
    }


    /// Writes a side-by-side preview to `path`, for reviewing a change to the
    /// glyph without installing the app.
    ///
    /// Renders it the way the bar actually will: at 1x, 2x and 3x, tinted for both
    /// a light and a dark menu bar. A template is only ever seen tinted, so judging
    /// one by the raw black silhouette is judging something the user never sees.
    ///
    ///     burn-o-meter --preview-menubar-icon /tmp/icon.png
    static func writePreview(to path: String) -> Bool {
        let scales: [CGFloat] = [1, 2, 3]
        let pad: CGFloat = 12
        let cell = height + pad
        let w = Int((cell * CGFloat(scales.count) + pad) * 3)
        let h = Int((cell * 2 + pad) * 3)

        guard let ctx = CGContext(
            data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else { return false }
        ctx.scaleBy(x: 3, y: 3)

        // Two bars, the colours macOS actually uses behind a status item.
        let bars: [(bg: CGColor, tint: CGColor)] = [
            (CGColor(red: 0.96, green: 0.96, blue: 0.97, alpha: 1),
             CGColor(red: 0, green: 0, blue: 0, alpha: 0.85)),
            (CGColor(red: 0.13, green: 0.13, blue: 0.14, alpha: 1),
             CGColor(red: 1, green: 1, blue: 1, alpha: 0.95)),
        ]

        for (row, bar) in bars.enumerated() {
            let y = pad / 2 + CGFloat(row) * cell
            ctx.setFillColor(bar.bg)
            ctx.fill(CGRect(x: 0, y: y - pad / 2, width: CGFloat(w) / 3, height: cell))
            for (col, scale) in scales.enumerated() {
                let x = pad / 2 + CGFloat(col) * cell
                // Render the glyph at its backing resolution, then place it at
                // point size - which is exactly what the window server does.
                let px = Int(height * scale)
                guard let glyph = CGContext(
                    data: nil, width: px, height: px, bitsPerComponent: 8, bytesPerRow: 0,
                    space: CGColorSpaceCreateDeviceRGB(),
                    bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
                ) else { continue }
                glyph.scaleBy(x: scale, y: scale)
                draw(in: glyph, box: CGRect(x: 0, y: 0, width: height, height: height))
                guard let mask = glyph.makeImage() else { continue }

                // A template is drawn as a mask filled with the bar's tint.
                ctx.saveGState()
                let place = CGRect(x: x, y: y, width: height, height: height)
                ctx.clip(to: place, mask: mask)
                ctx.setFillColor(bar.tint)
                ctx.fill(place)
                ctx.restoreGState()
            }
        }

        guard let out = ctx.makeImage(),
              let dest = CGImageDestinationCreateWithURL(
                  URL(fileURLWithPath: path) as CFURL, "public.png" as CFString, 1, nil)
        else { return false }
        CGImageDestinationAddImage(dest, out, nil)
        return CGImageDestinationFinalize(dest)
    }

    private static func draw(in ctx: CGContext, box: CGRect) {
        let black = CGColor(red: 0, green: 0, blue: 0, alpha: 1)
        func rad(_ deg: CGFloat) -> CGFloat { deg * .pi / 180 }

        // The dial. Heavier in proportion than the app icon's, because a hairline
        // arc disappears at 16pt on a non-Retina display.
        let centre = CGPoint(x: box.midX, y: box.minY + box.height * 0.32)
        let radius = box.width * 0.42
        let lineWidth = box.width * 0.115

        ctx.setLineCap(.round)
        ctx.setLineWidth(lineWidth)
        ctx.setStrokeColor(black)
        ctx.addArc(center: centre, radius: radius,
                   startAngle: rad(200), endAngle: rad(-20), clockwise: true)
        ctx.strokePath()

        // Narrow and tall. A flame whose width approaches its height stops being a
        // flame and becomes a dot in a ring - the tip is the whole silhouette.
        //
        // The height is bounded by where the arc's *inner* edge falls, not its
        // centreline: at 0.52 the tip landed exactly on that edge and the two fused
        // into a spike, which reads as a thermometer rather than a flame. The arc's
        // inner edge sits at (0.32 + 0.42 - 0.0575) = 0.68 of the box, so the tip
        // stops at 0.61 and keeps visible daylight under the apex.
        let flameW = box.width * 0.30
        let flameH = box.height * 0.45
        let flameBox = CGRect(x: box.midX - flameW / 2,
                              y: box.minY + box.height * 0.16,
                              width: flameW, height: flameH)

        // Cut the gap. Stroking the flame's own path in `.clear` erases a band of
        // arc around it, which is what a halo does in the colour version.
        ctx.saveGState()
        ctx.setBlendMode(.clear)
        ctx.addPath(flamePath(in: flameBox))
        ctx.setLineWidth(box.width * 0.10)
        ctx.setLineJoin(.round)
        ctx.strokePath()
        ctx.restoreGState()

        ctx.addPath(flamePath(in: flameBox))
        ctx.setFillColor(black)
        ctx.fillPath()
    }
}
