import XCTest
@testable import LuokeCore

/// 主窗口的「添加应用」页整个进程共用一个 AppsModel：上一件做完的那句要能收起来，还在做的不能打断
final class AppsModelResetTests: XCTestCase {
    func testResetDoneGoesBackToIdleAndDropsTheKeyboardHint() {
        let m = AppsModel()
        m.phase = .done("螺壳输入法装好了", true)
        m.imeAdded = true
        m.resetDone()
        XCTAssertEqual(m.phase, .idle)
        XCTAssertFalse(m.imeAdded)
    }

    func testResetDoneLeavesWorkInProgress() {
        let m = AppsModel()
        m.phase = .updating("luoke-mail")
        m.resetDone()
        XCTAssertEqual(m.phase, .updating("luoke-mail"))
    }
}
