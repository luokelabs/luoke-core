import XCTest
@testable import LuokeCore

final class FirstRunResumeTests: XCTestCase {
    private func ime(installed: Bool, selected: Bool) -> AccessoryChoice {
        AccessoryChoice(id: "luoke-ime", name: "螺壳输入法", bundleID: "ai.luoke.ime", type: "input-method", memory: false, size: 0,
                        installed: installed, unavailable: nil, selected: selected)
    }

    func testFirstRunShowsTheInputMethodOnceItIsInstalled() {
        XCTAssertTrue(FirstRunModel.showsInputMethod([ime(installed: false, selected: true)], upgrade: false))
        XCTAssertTrue(FirstRunModel.showsInputMethod([ime(installed: true, selected: false)], upgrade: false),
                      "重开后第二遍：输入法已经装上，不在可勾的里，完成页照样要出")
        XCTAssertFalse(FirstRunModel.showsInputMethod([ime(installed: false, selected: false)], upgrade: false))
    }

    func testUpgradeShowsOnlyANewlyInstalledInputMethod() {
        XCTAssertTrue(FirstRunModel.showsInputMethod([ime(installed: false, selected: true)], upgrade: true))
        XCTAssertFalse(FirstRunModel.showsInputMethod([ime(installed: true, selected: false)], upgrade: true))
    }

    func testReachedDoneIsRememberedUntilFinished() {
        let suite = "FirstRunResumeTests-\(UUID().uuidString)"
        let d = UserDefaults(suiteName: suite)!
        defer { d.removePersistentDomain(forName: suite) }
        XCTAssertNil(FirstRun.reachedDone(defaults: d))
        FirstRun.saveReachedDone(failed: ["engine"], inputMethod: true, defaults: d)
        XCTAssertEqual(FirstRun.reachedDone(defaults: d), FirstRun.ReachedDone(failed: ["engine"], inputMethod: true))
    }
}
