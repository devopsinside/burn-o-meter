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
/// **It is a template image.** macOS re-tints a template to suit the menu bar it
/// lands in — black on a light bar, white on a dark one, inverted again while the
/// item is clicked, and adjusted under Increase Contrast. A coloured image opts out
/// of every one of those. So the brand orange deliberately does not survive into
/// the bar; it is carried by the app icon and the popover, where there is a
/// background to own it.
///
/// **The image is built from the drawing, not the drawing tuned to the image.**
/// The shape is rasterised once, its ink measured, and the image then sized and
/// positioned so that ink lands where Apple puts the ink in an SF Symbol. Three
/// hand-tuned offsets failed before this, each a guess at a number that can simply
/// be read — and two of them "passed" a probe that was measuring the wrong thing
/// while a screenshot showed the icon plainly out of line with its neighbours.
enum MenuBarIcon {
    /// How tall the drawn shape should be, in points.
    ///
    /// Taken from Apple's artwork rather than chosen. An SF Symbol configured at
    /// the menu bar font's size renders 15pt square with its ink filling 0.896 of
    /// that — 13.4pt. `gauge.medium`, `speedometer` and `flame.fill` all agree to
    /// within a few thousandths; `--preview-menubar-icon` prints them.
    static let targetInkHeight: CGFloat = 13.4

    /// Where Apple centres the ink inside a symbol's box: the middle. Measured at
    /// 0.498–0.506 across their symbols.
    ///
    /// This is the correction that mattered. Every other item in the bar is placed
    /// this way, so an icon aligned instead to its own adjacent text — whose digits
    /// have no descender and therefore ride high — ends up standing out of line
    /// with all of them, which is exactly how it looked.
    static let targetInkCentre: Double = 0.5

    /// How far to drop the title so its ink shares the glyph's centre line.
    ///
    /// Negative moves text down. Measured, not guessed: on a 22pt button the glyph
    /// occupies rows 5…17 for a centre of 11.0 while "20%" occupies 5…15 for 10.0,
    /// and `--probe-alignment` reports the difference directly.
    static let titleBaselineOffset: CGFloat = -1.0

    /// Built once. `NSStatusItem` asks for this on every redraw.
    static let image: NSImage = make()

    /// The ink of `draw(in:)` as fractions of the box handed to it. Measured once.
    static let rawInk: (minX: Double, minY: Double, maxX: Double, maxY: Double) =
        measureRawInk() ?? (0, 0, 1, 1)

    private static func make() -> NSImage {
        // Scale the drawing until its ink is as tall as Apple's, then make the
        // image exactly that ink. With no slack around it the ink is centred by
        // construction, so AppKit centring the frame centres what is drawn — and
        // there is no offset left to get wrong.
        let inkH = max(CGFloat(rawInk.maxY - rawInk.minY), 0.001)
        let inkW = max(CGFloat(rawInk.maxX - rawInk.minX), 0.001)

        // Whole points, and the ink fitted to *those* rather than the other way
        // round. Sizing the image from the unrounded ink height and then rounding
        // it made the image shorter than what was drawn into it, so the top of the
        // arc was clipped off and the remainder measured as sitting low.
        let height = targetInkHeight.rounded()
        let side = height / inkH
        let width = (inkW * side).rounded()

        let size = NSSize(width: width, height: height)
        let image = NSImage(size: size, flipped: false) { rect in
            guard let ctx = NSGraphicsContext.current?.cgContext else { return false }
            // Place the box so the ink lands on the rect exactly, sharing whatever
            // the width rounding left over between the two sides.
            let dx = (rect.width - inkW * side) / 2
            draw(in: ctx, box: CGRect(x: rect.minX - CGFloat(rawInk.minX) * side + dx,
                                      y: rect.minY - CGFloat(rawInk.minY) * side,
                                      width: side, height: side))
            return true
        }
        image.isTemplate = true
        return image
    }

    // MARK: - Measurement

    /// Rasterise `draw(in:)` in a square box and find the extent of its ink.
    private static func measureRawInk(samples: Int = 512)
        -> (minX: Double, minY: Double, maxX: Double, maxY: Double)? {
        guard let ctx = bitmap(width: samples, height: samples) else { return nil }
        draw(in: ctx, box: CGRect(x: 0, y: 0, width: CGFloat(samples), height: CGFloat(samples)))
        return inkExtent(of: ctx)
    }

    /// Where the ink sits inside the *finished image*, as fractions of its size.
    ///
    /// The image rather than the raw drawing: what has to be right is how the thing
    /// AppKit places relates to its own frame, because that frame is what gets
    /// centred in the bar.
    static func inkBounds(samples: Int = 512)
        -> (minX: Double, minY: Double, maxX: Double, maxY: Double)? {
        guard let ctx = rasterise(scale: CGFloat(samples) / image.size.height) else { return nil }
        return inkExtent(of: ctx)
    }

    /// Where Apple puts the ink inside one of their own symbols, for reference.
    static func symbolInk(_ name: String) -> (minY: Double, maxY: Double, height: Double)? {
        let config = NSImage.SymbolConfiguration(pointSize: NSFont.menuBarFont(ofSize: 0).pointSize,
                                                 weight: .regular)
        guard let symbol = NSImage(systemSymbolName: name, accessibilityDescription: nil)?
            .withSymbolConfiguration(config) else { return nil }
        var rect = NSRect(origin: .zero, size: symbol.size)
        guard rect.width > 0, rect.height > 0,
              let cg = symbol.cgImage(forProposedRect: &rect, context: nil, hints: nil),
              let ctx = bitmap(width: cg.width * 8, height: cg.height * 8)
        else { return nil }
        ctx.draw(cg, in: CGRect(x: 0, y: 0, width: ctx.width, height: ctx.height))
        guard let ink = inkExtent(of: ctx) else { return nil }
        return (ink.minY, ink.maxY, ink.maxY - ink.minY)
    }

    private static func bitmap(width: Int, height: Int) -> CGContext? {
        guard width > 0, height > 0 else { return nil }
        return CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
                         bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                         bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
    }

    /// Bounding box of everything with meaningful alpha, as fractions of the canvas.
    private static func inkExtent(of ctx: CGContext)
        -> (minX: Double, minY: Double, maxX: Double, maxY: Double)? {
        guard let data = ctx.data else { return nil }
        let w = ctx.width, h = ctx.height
        let bytes = data.bindMemory(to: UInt8.self, capacity: h * ctx.bytesPerRow)
        var minX = w, minY = h, maxX = -1, maxY = -1
        for y in 0..<h {
            for x in 0..<w where bytes[y * ctx.bytesPerRow + x * 4 + 3] > 8 {
                if x < minX { minX = x }
                if x > maxX { maxX = x }
                if y < minY { minY = y }
                if y > maxY { maxY = y }
            }
        }
        guard maxX >= 0 else { return nil }
        // Rows are indexed from the top of the buffer while the drawing's y runs
        // upward, so the vertical extent is flipped back here. Returning it
        // unflipped silently reported the shape as sitting 0.083 higher than it
        // does - which, since that was the size of the offset being removed, looked
        // exactly like the offset still being applied.
        return (Double(minX) / Double(w), 1 - Double(maxY + 1) / Double(h),
                Double(maxX + 1) / Double(w), 1 - Double(minY) / Double(h))
    }

    /// The finished image, drawn into a bitmap at `scale`.
    private static func rasterise(scale: CGFloat) -> CGContext? {
        let size = image.size
        guard let ctx = bitmap(width: Int((size.width * scale).rounded()),
                               height: Int((size.height * scale).rounded())) else { return nil }
        ctx.scaleBy(x: scale, y: scale)
        let gc = NSGraphicsContext(cgContext: ctx, flipped: false)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = gc
        image.draw(in: NSRect(origin: .zero, size: size))
        NSGraphicsContext.restoreGraphicsState()
        return ctx
    }

    /// Writes a side-by-side preview to `path`, for reviewing a change to the glyph
    /// without installing the app.
    ///
    /// Renders the real image at 1x, 2x and 3x, tinted for both a light and a dark
    /// menu bar. A template is only ever seen tinted, so judging one by its raw
    /// black silhouette is judging something the user never sees.
    static func writePreview(to path: String) -> Bool {
        let scales: [CGFloat] = [1, 2, 3]
        let pad: CGFloat = 12
        let size = image.size
        let cellW = size.width + pad, cellH = size.height + pad
        let w = Int((cellW * CGFloat(scales.count) + pad) * 3)
        let h = Int((cellH * 2 + pad) * 3)
        guard let ctx = bitmap(width: w, height: h) else { return false }
        ctx.scaleBy(x: 3, y: 3)

        let bars: [(bg: CGColor, tint: CGColor)] = [
            (CGColor(red: 0.96, green: 0.96, blue: 0.97, alpha: 1),
             CGColor(red: 0, green: 0, blue: 0, alpha: 0.85)),
            (CGColor(red: 0.13, green: 0.13, blue: 0.14, alpha: 1),
             CGColor(red: 1, green: 1, blue: 1, alpha: 0.95)),
        ]

        for (row, bar) in bars.enumerated() {
            let y = pad / 2 + CGFloat(row) * cellH
            ctx.setFillColor(bar.bg)
            ctx.fill(CGRect(x: 0, y: y - pad / 2, width: CGFloat(w) / 3, height: cellH))
            for (col, scale) in scales.enumerated() {
                let x = pad / 2 + CGFloat(col) * cellW
                guard let glyph = rasterise(scale: scale), let mask = glyph.makeImage() else {
                    continue
                }
                ctx.saveGState()
                let place = CGRect(x: x, y: y, width: size.width, height: size.height)
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

    // MARK: - The shape

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

    /// The glyph, in whatever box it is given. Carries no positioning of its own:
    /// where it ends up is decided by `make()`, from the ink this produces.
    private static func draw(in ctx: CGContext, box: CGRect) {
        let black = CGColor(red: 0, green: 0, blue: 0, alpha: 1)
        func rad(_ deg: CGFloat) -> CGFloat { deg * .pi / 180 }

        // The dial. Heavier in proportion than the app icon's, because a hairline
        // arc disappears at this size on a non-Retina display.
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
        // flame and becomes a dot in a ring — the tip is the whole silhouette.
        //
        // The height is bounded by where the arc's *inner* edge falls, not its
        // centreline: at 0.52 the tip landed exactly on that edge and the two fused
        // into a spike, which reads as a thermometer. The inner edge sits at
        // (0.32 + 0.42 - 0.0575) = 0.68 of the box, so the tip stops at 0.61 and
        // keeps visible daylight under the apex.
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
