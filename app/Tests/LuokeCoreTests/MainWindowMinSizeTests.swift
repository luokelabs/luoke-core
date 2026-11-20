import AppKit
import SwiftUI
import XCTest
@testable import LuokeCore

/// 主窗口最小内容 860×600（luoke-core#230）：NSHostingView 会按 SwiftUI 内容的最小尺寸改写窗口的 contentMinSize，
/// 侧栏以外都能缩到 0 的内容就让窗口拉到约 240 宽。拖的时候由 windowWillResize 卡住（MainWindowHost.clamp）
final class MainWindowMinSizeTests: XCTestCase {
    private func window() -> NSWindow {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1004, height: 760),
                         styleMask: [.titled, .closable, .resizable, .fullSizeContentView], backing: .buffered, defer: false)
        w.isReleasedWhenClosed = false
        w.contentView = NSHostingView(rootView: HStack { Color.clear.frame(width: 240); Color.clear.frame(minWidth: 0, maxWidth: .infinity) })
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))     // SwiftUI 布局一轮（窗口不显示）
        return w
    }

    /// 这就是 bug：只设 contentMinSize 留不住
    func testHostingViewOverwritesContentMinSize() {
        let w = window()
        w.contentMinSize = MainWindowHost.minSize
        RunLoop.main.run(until: Date().addingTimeInterval(0.3))
        w.setContentSize(NSSize(width: 300, height: 400))
        RunLoop.main.run(until: Date().addingTimeInterval(0.3))
        XCTAssertLessThan(w.contentMinSize.width, 860)
    }

    func testDraggingNarrowerStopsAt860x600() {
        let w = window()
        let floor = w.frameRect(forContentRect: NSRect(origin: .zero, size: MainWindowHost.minSize)).size
        XCTAssertEqual(MainWindowHost.clamp(NSSize(width: 240, height: 200), in: w), floor)
        XCTAssertEqual(MainWindowHost.clamp(NSSize(width: 1200, height: 200), in: w), NSSize(width: 1200, height: floor.height))
        XCTAssertEqual(MainWindowHost.clamp(NSSize(width: 1200, height: 900), in: w), NSSize(width: 1200, height: 900))
        XCTAssertEqual(w.contentRect(forFrameRect: NSRect(origin: .zero, size: floor)).size, MainWindowHost.minSize)
    }

    func testSavedNarrowFrameIsWidenedKeepingTopLeft() {
        let w = window()
        w.setFrame(NSRect(x: 100, y: 100, width: 243, height: 700), display: false)
        let top = w.frame.maxY
        MainWindowHost.enforce(on: w)
        XCTAssertEqual(w.contentRect(forFrameRect: w.frame).width, 860)
        XCTAssertEqual(w.frame.maxY, top)
        XCTAssertEqual(w.frame.minX, 100)
    }
}
