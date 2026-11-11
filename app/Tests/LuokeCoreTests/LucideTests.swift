import AppKit
import SwiftUI
import XCTest
@testable import LuokeCore

/// 本体窗口的图标都是 Lucide（app/Resources/lucide，build_app.sh 拷进 Contents/Resources/lucide）：
/// 代码里用到的名字都带着 SVG、带着的都用得到；除了菜单栏图标，代码里不再有 SF Symbols。
final class LucideTests: XCTestCase {
    private let app = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    private var sources: URL { app.appendingPathComponent("Sources/LuokeCore", isDirectory: true) }
    private var lucide: URL { app.appendingPathComponent("Resources/lucide", isDirectory: true) }

    private func swiftSources() throws -> [(name: String, text: String)] {
        let files = try FileManager.default.contentsOfDirectory(at: sources, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "swift" }.sorted { $0.lastPathComponent < $1.lastPathComponent }
        XCTAssertFalse(files.isEmpty, sources.path)
        return try files.map { ($0.lastPathComponent, try String(contentsOf: $0, encoding: .utf8)) }
    }

    private func bundled() throws -> Set<String> {
        let files = try FileManager.default.contentsOfDirectory(at: lucide, includingPropertiesForKeys: nil)
        return Set(files.filter { $0.pathExtension == "svg" }.map { $0.deletingPathExtension().lastPathComponent })
    }

    /// 代码里写到的图标名：`LKIcon(`、`menuImage(` 后面和 `icon:` 后面那个参数里的字符串（到同一层的逗号、右括号或换行为止，
    /// 三元表达式里的两个都算）
    static func iconNames(in text: String) -> [String] {
        let chars = Array(text)
        let starts = ["LKIcon(", "menuImage(", "icon:"].map(Array.init)
        func word(_ c: Character) -> Bool { c.isLetter || c.isNumber || c == "_" }
        var names: [String] = []
        var i = 0
        while i < chars.count {
            guard let s = starts.first(where: { s in i + s.count <= chars.count && Array(chars[i..<i + s.count]) == s }),
                  i == 0 || !word(chars[i - 1]) else { i += 1; continue }
            var j = i + s.count, depth = 0, literal: String?
            scan: while j < chars.count {
                let c = chars[j]
                if var lit = literal {
                    if c == "\\" { j += 2; continue }
                    if c == "\"" { names.append(lit); literal = nil } else { lit.append(c); literal = lit }
                } else {
                    switch c {
                    case "\"": literal = ""
                    case "(", "[", "{": depth += 1
                    case ")", "]", "}": if depth == 0 { break scan }; depth -= 1
                    case ",", "\n": if depth == 0 { break scan }
                    default: break
                    }
                }
                j += 1
            }
            i = j
        }
        return names
    }

    func testTheNameScanner() {
        let sample = """
        LKNote(icon: ok ? "circle-check" : "triangle-alert", text: "没装上，重试")
        LKIcon("check", size: 11).foregroundStyle(.white)
        Button {} label: { LKLabel("重试", icon: "rotate-cw") }
        let rows: [(icon: String, title: String)] = [(icon: "brain", title: "记忆")]
        } label: { Lucide.menuImage("ellipsis") }
        LKAppIcon(accessory: "luoke-core", size: 48)
        """
        XCTAssertEqual(Self.iconNames(in: sample), ["circle-check", "triangle-alert", "check", "rotate-cw", "brain", "ellipsis"])
    }

    func testEveryIconUsedIsBundled() throws {
        let have = try bundled()
        var used = 0
        for (name, text) in try swiftSources() {
            for icon in Self.iconNames(in: text) {
                used += 1
                XCTAssertTrue(have.contains(icon), "\(name) 用了 \(icon)，app/Resources/lucide 里没有 \(icon).svg")
            }
        }
        XCTAssertGreaterThan(used, 30)
    }

    /// 只放用到的图标
    func testEveryBundledIconIsUsed() throws {
        let used = Set(try swiftSources().flatMap { Self.iconNames(in: $0.text) })
        for icon in try bundled().sorted() {
            XCTAssertTrue(used.contains(icon), "app/Resources/lucide/\(icon).svg 没人用，删掉")
        }
    }

    /// 每个 SVG 都是 lucide-static 1.64.0 原样的，画得出来、是模板图；许可也在
    func testIconsAreLucideAndDraw() throws {
        for icon in try bundled().sorted() {
            let svg = try String(contentsOf: lucide.appendingPathComponent(icon + ".svg"), encoding: .utf8)
            XCTAssertTrue(svg.hasPrefix("<!-- @license lucide-static v1.64.0 - ISC -->"), icon)
            XCTAssertTrue(svg.contains("class=\"lucide lucide-\(icon)\""), icon)
            let img = try XCTUnwrap(Lucide.image(icon, in: lucide), icon)
            XCTAssertTrue(img.isTemplate, icon)
            let rep = try XCTUnwrap(NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 32, pixelsHigh: 32, bitsPerSample: 8, samplesPerPixel: 4,
                                                     hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0))
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
            img.draw(in: NSRect(x: 0, y: 0, width: 32, height: 32))
            NSGraphicsContext.restoreGraphicsState()
            let inked = (0..<32).contains { x in (0..<32).contains { y in (rep.colorAt(x: x, y: y)?.alphaComponent ?? 0) > 0.5 } }
            XCTAssertTrue(inked, "\(icon).svg 什么都没画出来")
        }
        let license = try String(contentsOf: lucide.appendingPathComponent("LICENSE"), encoding: .utf8)
        XCTAssertTrue(license.hasPrefix("ISC License"))
        XCTAssertNil(Lucide.image("../ui/logo", in: lucide), "名字只认 Lucide 的写法")
        XCTAssertNil(Lucide.image("no-such-icon", in: lucide))
    }

    /// 窗口里画出来是跟着字色的模板图，大小是给的大小（SF Symbols 跟着 .foregroundStyle 变色，换了以后也要）
    @MainActor
    func testIconTakesTheForegroundColorAndSize() throws {
        setenv("LUOKE_UI_ASSETS", app.appendingPathComponent("Resources/ui").path, 1)     // 和截图时一样：没有 .app，从 app/Resources 找
        defer { unsetenv("LUOKE_UI_ASSETS") }
        XCTAssertEqual(Lucide.dir?.standardizedFileURL, lucide.standardizedFileURL)
        func render(_ name: String, size: CGFloat) throws -> (red: Int, dark: Int, box: NSRect) {
            let host = NSHostingView(rootView: LKIcon(name, size: size).foregroundStyle(Color(red: 1, green: 0, blue: 0))
                .frame(width: 48, height: 48).background(Color.white))
            host.frame = NSRect(x: 0, y: 0, width: 48, height: 48)
            host.layoutSubtreeIfNeeded()
            let rep = try XCTUnwrap(host.bitmapImageRepForCachingDisplay(in: host.bounds))
            host.cacheDisplay(in: host.bounds, to: rep)
            var red = 0, dark = 0, box = NSRect.null
            let scale = CGFloat(rep.pixelsWide) / 48
            for x in 0..<rep.pixelsWide {
                for y in 0..<rep.pixelsHigh {
                    guard let c = rep.colorAt(x: x, y: y)?.usingColorSpace(.sRGB) else { continue }
                    if c.redComponent > 0.8 && c.greenComponent < 0.5 && c.blueComponent < 0.5 {
                        red += 1
                        box = box.union(NSRect(x: CGFloat(x) / scale, y: CGFloat(y) / scale, width: 1 / scale, height: 1 / scale))
                    }
                    if c.redComponent < 0.3 && c.greenComponent < 0.3 && c.blueComponent < 0.3 { dark += 1 }
                }
            }
            return (red, dark, box)
        }
        let small = try render("square-arrow-out-up-right", size: 16), big = try render("square-arrow-out-up-right", size: 32)
        XCTAssertGreaterThan(small.red, 20, "图标画成了字色")
        XCTAssertEqual(small.dark, 0, "没有按 SVG 原来的黑色画")
        // Lucide 的图在 24 格里留 3 格边：16 pt 的图实际约 12～14 pt 宽，32 pt 的约两倍
        XCTAssertEqual(small.box.width, 13.3, accuracy: 2)
        XCTAssertEqual(big.box.width, 26.7, accuracy: 3)
        XCTAssertEqual(try render("no-such-icon", size: 16).red, 0, "图没带上：空着，不画别的")
    }

    /// 菜单栏图标（MenuBar.icon 找不到自己的图时的退路）以外，不用 SF Symbols
    func testNoSFSymbolsOutsideTheMenuBarIcon() throws {
        var hits: [String] = []
        for (name, text) in try swiftSources() {
            for (n, line) in text.components(separatedBy: "\n").enumerated()
            where ["systemName:", "systemImage:", "systemSymbolName:"].contains(where: { line.contains($0) }) {
                hits.append("\(name):\(n + 1)")
                if name == "MenuBar.swift" { XCTAssertTrue(line.contains("NSImage(systemSymbolName: \"circle\""), line) }
            }
        }
        XCTAssertEqual(hits.filter { !$0.hasPrefix("MenuBar.swift:") }, [])
        XCTAssertEqual(hits.filter { $0.hasPrefix("MenuBar.swift:") }.count, 1, hits.joined(separator: " "))
    }

    /// 打包时拷进 Contents/Resources/lucide（Lucide.dir 在那里找）
    func testBuildScriptBundlesTheIcons() throws {
        let script = try String(contentsOf: app.appendingPathComponent("build_app.sh"), encoding: .utf8)
        XCTAssertTrue(script.contains("cp -R \"$here/Resources/lucide\" \"$app/Contents/Resources/lucide\""))
        XCTAssertEqual(Lucide.folder, "lucide")
    }
}
