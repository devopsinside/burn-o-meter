#!/usr/bin/env swift
//
// Generates macos/burn-o-meter.iconset and the .icns from it.
//
// Drawn in code rather than committed as binary art so it stays reviewable and
// reproducible: a pull request can see what changed about the icon, and anyone
// can regenerate every size from one source of truth.
//
//   swift macos/make-icon.swift
//
import AppKit
import CoreGraphics
import Foundation

// Brand orange, the same hue the charts use for Claude, into a deeper red so the
// icon reads as heat rather than as a flat colour chip.
let top    = CGColor(red: 0.98, green: 0.55, blue: 0.20, alpha: 1)   // #FA8C33
let bottom = CGColor(red: 0.83, green: 0.24, blue: 0.13, alpha: 1)   // #D43D21

/// A flame, in a 0...1 box. Built from curves rather than a font glyph so it is
/// identical on every machine and at every size.
func flamePath(in rect: CGRect) -> CGPath {
    let p = CGMutablePath()
    func pt(_ x: CGFloat, _ y: CGFloat) -> CGPoint {
        CGPoint(x: rect.minX + x * rect.width, y: rect.minY + y * rect.height)
    }
    // A rounded base carrying a wide belly, pulled into a waist and finishing in a
    // sharp tip. The tip is what makes it read as fire rather than as a leaf, so it
    // is a near-cusp rather than a curve.
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

/// The meter: a dial sweeping over the top, sitting low in the icon so the flame
/// can stand over it rather than be crowded inside it.
///
/// The filled portion stops short of the end deliberately — a dial reading high but
/// not pinned says "this is a measurement", where a full one would just read as a
/// progress bar.
func drawGauge(_ ctx: CGContext, in box: CGRect, size: Int) {
    let centre = CGPoint(x: box.midX, y: box.minY + box.height * 0.335)
    let radius = box.width * 0.385
    let width = box.width * (size < 64 ? 0.105 : 0.088)

    func rad(_ deg: CGFloat) -> CGFloat { deg * .pi / 180 }

    ctx.setLineCap(.round)
    ctx.setLineWidth(width)

    // Unfilled track.
    ctx.setStrokeColor(CGColor(red: 1, green: 0.97, blue: 0.92, alpha: 0.33))
    ctx.addArc(center: centre, radius: radius,
               startAngle: rad(200), endAngle: rad(-20), clockwise: true)
    ctx.strokePath()

    // Filled portion.
    ctx.setStrokeColor(CGColor(red: 1, green: 0.98, blue: 0.94, alpha: 1))
    ctx.addArc(center: centre, radius: radius,
               startAngle: rad(200), endAngle: rad(28), clockwise: true)
    ctx.strokePath()
}

func drawIcon(size: Int) -> Data? {
    let s = CGFloat(size)
    guard let ctx = CGContext(
        data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else { return nil }

    // macOS app icons sit on a rounded square, inset from the canvas so the
    // shadow the system draws has somewhere to go.
    let pad = s * 0.06
    let box = CGRect(x: pad, y: pad, width: s - pad * 2, height: s - pad * 2)
    let radius = box.width * 0.2237   // Apple's continuous-corner proportion
    ctx.saveGState()
    ctx.addPath(CGPath(roundedRect: box, cornerWidth: radius, cornerHeight: radius,
                       transform: nil))
    ctx.clip()
    if let gradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                                 colors: [top, bottom] as CFArray, locations: [0, 1]) {
        ctx.drawLinearGradient(gradient, start: CGPoint(x: 0, y: box.maxY),
                               end: CGPoint(x: 0, y: box.minY), options: [])
    }
    ctx.restoreGState()

    drawGauge(ctx, in: box, size: size)

    // The flame stands over the dial, overlapping its top arc. A halo in the
    // background colour is stroked first so the flame reads as being in front of
    // the meter rather than fused into it — without that the two silhouettes merge
    // into one shape at small sizes.
    // Sized to sit wholly inside the dial, with daylight above its tip: the arc
    // stays an unbroken curve, and the flame reads as the thing being measured.
    // Small sizes need a simpler drawing, not a smaller one. Below 64pt the dial's
    // detail is already gone and a proportionally-scaled flame collapses into an
    // indistinct dot, so the flame takes more of the frame and carries the icon.
    let small = size < 64
    let flameW = box.width * (small ? 0.40 : 0.30)
    let flameH = box.height * (small ? 0.50 : 0.375)
    let flameBox = CGRect(x: box.midX - flameW / 2,
                          y: box.minY + box.height * (small ? 0.245 : 0.275),
                          width: flameW, height: flameH)
    let flame = flamePath(in: flameBox)

    ctx.addPath(flame)
    ctx.setLineWidth(box.width * (small ? 0.055 : 0.035))
    ctx.setLineJoin(.round)
    ctx.setStrokeColor(CGColor(red: 0.86, green: 0.33, blue: 0.16, alpha: 1))
    ctx.strokePath()

    ctx.addPath(flame)
    ctx.setFillColor(CGColor(red: 1, green: 0.98, blue: 0.94, alpha: 1))
    ctx.fillPath()

    // A hotter core, only meaningful once there are pixels to show it.
    if size >= 64 {
        let inner = flameBox.insetBy(dx: flameW * 0.30, dy: 0)
        ctx.addPath(flamePath(in: CGRect(x: inner.minX, y: flameBox.minY + flameH * 0.05,
                                         width: inner.width, height: flameH * 0.58)))
        ctx.setFillColor(CGColor(red: 0.97, green: 0.52, blue: 0.16, alpha: 1))
        ctx.fillPath()
    }

    guard let image = ctx.makeImage() else { return nil }
    let rep = NSBitmapImageRep(cgImage: image)
    return rep.representation(using: .png, properties: [:])
}

// The ten entries macOS expects in an iconset.
let variants: [(String, Int)] = [
    ("icon_16x16", 16), ("icon_16x16@2x", 32),
    ("icon_32x32", 32), ("icon_32x32@2x", 64),
    ("icon_128x128", 128), ("icon_128x128@2x", 256),
    ("icon_256x256", 256), ("icon_256x256@2x", 512),
    ("icon_512x512", 512), ("icon_512x512@2x", 1024),
]

let root = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
let iconset = root.appendingPathComponent("macos/burn-o-meter.iconset")
try? FileManager.default.removeItem(at: iconset)
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)

for (name, px) in variants {
    guard let png = drawIcon(size: px) else {
        FileHandle.standardError.write("failed to render \(name)\n".data(using: .utf8)!)
        exit(1)
    }
    try png.write(to: iconset.appendingPathComponent("\(name).png"))
}
print("wrote \(variants.count) sizes to macos/burn-o-meter.iconset")
