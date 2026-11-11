import AppKit
import SwiftUI
import XCTest
@testable import LuokeCore

/// 检查更新窗口、升级完成页共用的卡片：说明再长，也只有中间的正文滚，标题和底栏的按钮留在窗口里。
/// 窗口不能调大小，升级完成页没有关闭按钮，按钮被挤出去就关不掉了。
@MainActor
final class UpgradeCardLayoutTests: XCTestCase {
    private let footerHeight: CGFloat = 64
    private let windowHeight: CGFloat = 460

    private func scrollViews(in view: NSView) -> [NSScrollView] {
        (view as? NSScrollView).map { [$0] } ?? view.subviews.flatMap(scrollViews)
    }

    /// 把带这份说明的卡片放进一个真窗口，等 SwiftUI 排好版，返回正文的滚动区
    private func bodyScroll(notes: String, keep window: inout NSWindow?) throws -> NSScrollView {
        let card = UpgradeCard(title: "有新版本") { LKReleaseNotes(text: notes) } footer: { Button("完成") {} }
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: windowHeight), styleMask: [.titled], backing: .buffered, defer: false)
        w.isReleasedWhenClosed = false
        w.contentView = NSHostingView(rootView: card)
        w.contentView?.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.3))
        window = w
        let all = scrollViews(in: try XCTUnwrap(w.contentView))
        XCTAssertEqual(all.count, 1, "正文只该有一个滚动区")
        return try XCTUnwrap(all.first)
    }

    private func assertBodyScrollsBetweenHeaderAndFooter(_ notes: String, file: StaticString = #filePath, line: UInt = #line) throws {
        var window: NSWindow?
        defer { window?.close() }
        let s = try bodyScroll(notes: notes, keep: &window)
        let frame = s.convert(s.bounds, to: nil)
        XCTAssertEqual(frame.minY, footerHeight, accuracy: 1, "滚动区要停在底栏上面", file: file, line: line)
        XCTAssertLessThan(frame.maxY, windowHeight - 60, "标题要留在滚动区上面", file: file, line: line)
        let doc = try XCTUnwrap(s.documentView).frame.height
        XCTAssertGreaterThan(doc, s.contentView.bounds.height, "这份说明应该放不下，要靠滚", file: file, line: line)
    }

    /// 只有 6 段，但每段都长：段数看不出高度
    func testFewLongParagraphsScroll() throws {
        let long = String(repeating: "这一段很长，", count: 60)
        try assertBodyScrollsBetweenHeaderAndFooter((1...6).map { "- \($0)：\(long)" }.joined(separator: "\n"))
    }

    func testManyShortLinesScroll() throws {
        try assertBodyScrollsBetweenHeaderAndFooter((1...40).map { "- 第 \($0) 条" }.joined(separator: "\n"))
    }

    func testShortNotesFitWithoutScrolling() throws {
        var window: NSWindow?
        defer { window?.close() }
        let s = try bodyScroll(notes: "- 一条\n- 两条", keep: &window)
        XCTAssertLessThanOrEqual(try XCTUnwrap(s.documentView).frame.height, s.contentView.bounds.height)
    }
}
