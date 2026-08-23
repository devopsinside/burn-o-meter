#!/usr/bin/env swift
//
// Generates the GitHub social preview card (1280x640).
//
// GitHub repositories have no icon - the owner's avatar is shown instead - so the
// image a repository can actually carry is the Open Graph card that appears when
// its link is shared. Same artwork as the app icon, so the two read as one project.
//
//   swift macos/make-social-preview.swift
//
import AppKit
import CoreGraphics
import Foundation

let W = 1280, H = 640

guard let ctx = CGContext(
    data: nil, width: W, height: H, bitsPerComponent: 8, bytesPerRow: 0,
    space: CGColorSpaceCreateDeviceRGB(),
    // RGBA, matching the format of GitHub's own repo-card template exactly
    // (colour-type 6). Their processor rejects a colour-type 2 image: the upload is
    // accepted, an asset id appears in the og:image tag, and that URL then 404s.
    // Every pixel is painted opaque below, so the alpha channel exists but is
    // uniformly 255 — a transparent card renders as nothing on a dark page.
    bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
) else { exit(1) }

// Paint the whole canvas opaque before anything else, so no pixel can retain the
// zero alpha the context is initialised with.
ctx.setBlendMode(.copy)
ctx.setFillColor(CGColor(red: 0.09, green: 0.09, blue: 0.10, alpha: 1))
ctx.fill(CGRect(x: 0, y: 0, width: CGFloat(W), height: CGFloat(H)))
ctx.setBlendMode(.normal)

// Dark ground, so the orange reads as heat rather than as a background wash, and
// so the card sits comfortably in both light and dark timelines.
ctx.setFillColor(CGColor(red: 0.09, green: 0.09, blue: 0.10, alpha: 1))
ctx.fill(CGRect(x: 0, y: 0, width: W, height: H))

// The icon, drawn large on the left.
let iconURL = URL(fileURLWithPath: "macos/burn-o-meter.iconset/icon_512x512@2x.png")
if let data = try? Data(contentsOf: iconURL),
   let src = CGImageSourceCreateWithData(data as CFData, nil),
   let image = CGImageSourceCreateImageAtIndex(src, 0, nil) {
    let side: CGFloat = 340
    ctx.draw(image, in: CGRect(x: 96, y: CGFloat(H) / 2 - side / 2, width: side, height: side))
}

func draw(_ text: String, x: CGFloat, y: CGFloat, size: CGFloat,
          weight: NSFont.Weight, color: CGColor) {
    let font = NSFont.systemFont(ofSize: size, weight: weight)
    let attrs: [NSAttributedString.Key: Any] = [
        .font: font, .foregroundColor: NSColor(cgColor: color) ?? .white,
    ]
    let line = CTLineCreateWithAttributedString(
        NSAttributedString(string: text, attributes: attrs)
    )
    ctx.textPosition = CGPoint(x: x, y: y)
    CTLineDraw(line, ctx)
}

let cream = CGColor(red: 1, green: 0.98, blue: 0.94, alpha: 1)
let muted = CGColor(red: 0.68, green: 0.68, blue: 0.70, alpha: 1)
let orange = CGColor(red: 0.98, green: 0.55, blue: 0.20, alpha: 1)

draw("burn-o-meter", x: 500, y: 400, size: 86, weight: .bold, color: cream)
draw("See what your AI coding agents really cost.", x: 502, y: 330, size: 36,
     weight: .regular, color: muted)
draw("Tokens · spend · cache · rate limits", x: 502, y: 268, size: 32,
     weight: .medium, color: orange)
// Matches the README exactly. "No network" stopped being true once a manual
// update check existed, and a marketing card is the last place to keep a claim
// the documentation has already corrected.
draw("Menu bar app and CLI. Runs entirely on your machine —", x: 502, y: 200,
     size: 27, weight: .regular, color: muted)
draw("no account, no telemetry, nothing sent anywhere.", x: 502, y: 162, size: 27,
     weight: .regular, color: muted)

guard let image = ctx.makeImage() else { exit(1) }
let rep = NSBitmapImageRep(cgImage: image)
guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }

// Keep only the chunks a decoder needs. AppKit attaches an eXIf chunk, and EXIF
// metadata inside a PNG is unusual enough that GitHub's social-preview processor
// rejects the image — it accepts the upload, then serves a 404 for it, which looks
// exactly like the upload having silently failed.
func stripAncillaryChunks(_ data: Data) -> Data {
    let keep: Set<String> = ["IHDR", "PLTE", "IDAT", "IEND"]
    var out = Data(data[0..<8])            // signature
    var i = 8
    while i + 8 <= data.count {
        let len = data[i..<i+4].reduce(0) { Int($0) << 8 | Int($1) }
        let type = String(bytes: data[i+4..<i+8], encoding: .ascii) ?? ""
        let end = i + 12 + len
        if end > data.count { break }
        if keep.contains(type) { out.append(data[i..<end]) }
        i = end
    }
    return out
}

try stripAncillaryChunks(png).write(to: URL(fileURLWithPath: "docs/social-preview.png"))
print("wrote docs/social-preview.png (\(W)x\(H))")
