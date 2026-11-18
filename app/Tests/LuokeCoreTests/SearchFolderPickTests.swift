import XCTest
@testable import LuokeCore

/// 搜索页的「添加」：本体弹选文件夹的框，把选中的加进搜索的设置。只用临时文件夹和假的设置、读取，不碰真的个人文件夹和搜索
final class SearchFolderPickTests: XCTestCase {
    private var home: URL!

    override func setUpWithError() throws {
        home = FileManager.default.temporaryDirectory.appendingPathComponent("sfp-home-\(UUID().uuidString)")
        for sub in ["Desktop/项目", "Documents", "别处/旧的"] {
            try FileManager.default.createDirectory(at: home.appendingPathComponent(sub), withIntermediateDirectories: true)
        }
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: home)
    }

    private func url(_ sub: String) -> URL { home.appendingPathComponent(sub) }

    /// 没刚在搜索的页上点过：搜索说什么都不弹（点螺壳里别的地方、底下输入框里按回车，都不告诉这里）
    func testNoSearchTapNoPanel() {
        XCTAssertFalse(SearchFolderPick.show(tapped: { nil }, recent: { true }))
        XCTAssertFalse(SearchFolderPick.picking)
        XCTAssertEqual(SearchFolderPick.refusal(tapped: nil, recent: true, picking: false, closed: nil, now: 100), .noTap)
    }

    /// 搜索说一次要选，那一下就用掉了，弹不弹都算：搜索再说（连着说、出错重发）不弹
    func testATapIsUsedOnce() {
        SearchFolderPick.searchTapped()
        XCTAssertNotNil(SearchFolderPick.lastSearchTap)
        XCTAssertFalse(SearchFolderPick.show(recent: { false }), "螺壳不在最前：不弹")
        XCTAssertNil(SearchFolderPick.lastSearchTap, "没弹也用掉了")
        XCTAssertFalse(SearchFolderPick.show(recent: { true }), "再说一次：没有新的点按")
    }

    /// 点了以后几秒内才算；时间倒着的不算
    func testTapGoesStale() {
        let w = SearchFolderPick.tapWait
        XCTAssertNil(SearchFolderPick.refusal(tapped: 100, recent: true, picking: false, closed: nil, now: 100.5))
        XCTAssertEqual(SearchFolderPick.refusal(tapped: 100, recent: true, picking: false, closed: nil, now: 100 + w), .noTap)
        XCTAssertEqual(SearchFolderPick.refusal(tapped: 100, recent: true, picking: false, closed: nil, now: 99), .noTap)
    }

    /// 螺壳不在最前、没刚有过点按按键：不弹
    func testNeedsLuokeInFront() {
        XCTAssertEqual(SearchFolderPick.refusal(tapped: 100, recent: false, picking: false, closed: nil, now: 101), .notFront)
    }

    /// 框开着、选完还在写设置：不再弹
    func testNotWhilePicking() {
        XCTAssertEqual(SearchFolderPick.refusal(tapped: 100, recent: true, picking: true, closed: nil, now: 101), .picking)
        XCTAssertNil(SearchFolderPick.refusal(tapped: 100, recent: true, picking: false, closed: nil, now: 101))
    }

    /// 上一个框、卡片关掉以后，用户要在搜索的页上再点一下才弹（不然搜索一直说，框就一直回来）
    func testNeedsATapAfterTheLastClose() {
        XCTAssertEqual(SearchFolderPick.refusal(tapped: 100, recent: true, picking: false, closed: 101, now: 102), .beforeClose, "点在关掉之前")
        XCTAssertEqual(SearchFolderPick.refusal(tapped: 101, recent: true, picking: false, closed: 101, now: 102), .beforeClose, "同一刻")
        XCTAssertNil(SearchFolderPick.refusal(tapped: 101.5, recent: true, picking: false, closed: 101, now: 102), "关掉以后又点了")
    }

    /// 只有和设置里的某一个是同一个才算已经在搜（跟着链接比）；在里面的、名字开头一样的都不算
    func testSame() throws {
        let h = home.path
        XCTAssertTrue(SearchFolderPick.same(h, [h]))
        XCTAssertTrue(SearchFolderPick.same(h + "/Desktop/", [h + "/Desktop"]))
        XCTAssertFalse(SearchFolderPick.same(h + "/Desktop/项目", [h]), "在已搜的里面的照样加：搜索平常可能跳过它")
        XCTAssertFalse(SearchFolderPick.same(h + "/Desktop2", [h + "/Desktop"]))
        let link = FileManager.default.temporaryDirectory.appendingPathComponent("sfp-link-\(UUID().uuidString)")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: home)
        defer { try? FileManager.default.removeItem(at: link) }
        XCTAssertTrue(SearchFolderPick.same(h + "/Documents", [link.path + "/Documents"]))
    }

    /// 加进去的只有选的那一个，已搜的一项也不去掉；读的也只有选的这个
    func testAddsOnlyThePick() {
        let h = home.path
        var asked: [String] = [], chose: [String]?
        let outcome = SearchFolderPick.add(url("别处"), saved: { [h + "/Desktop", h + "/别处/旧的"] },
                                           canRead: { asked.append($0.path); return true },
                                           choose: { chose = $0; return nil })
        XCTAssertEqual(outcome, .added)
        XCTAssertEqual(chose, [h + "/别处"])
        XCTAssertEqual(asked, [h + "/别处"])
    }

    /// 已搜文件夹里面的一个：照样加成单独一项
    func testAddsAFolderInsideASearchedOne() {
        let h = home.path
        var chose: [String]?
        let outcome = SearchFolderPick.add(url("Desktop/项目"), saved: { [h] }, canRead: { _ in true },
                                           choose: { chose = $0; return nil })
        XCTAssertEqual(outcome, .added)
        XCTAssertEqual(chose, [h + "/Desktop/项目"])
    }

    /// 已经在搜的：不读、不写，说一声
    func testAlreadySearched() {
        let h = home.path
        var wrote = false
        let outcome = SearchFolderPick.add(url("Desktop/项目"), saved: { [h + "/Desktop/项目"] },
                                           canRead: { _ in XCTFail("不该读"); return true }, choose: { _ in wrote = true; return nil })
        XCTAssertEqual(outcome, .already("项目"))
        XCTAssertFalse(wrote)
        XCTAssertEqual(SearchFolderPick.text(outcome)?.text, "「项目」已经在搜了。")
    }

    /// macOS 没让读：不写，卡片带「打开系统设置」
    func testDenied() {
        var wrote = false
        let outcome = SearchFolderPick.add(url("Desktop"), saved: { [] }, canRead: { _ in false },
                                           choose: { _ in wrote = true; return nil })
        XCTAssertEqual(outcome, .denied(FileManager.default.displayName(atPath: url("Desktop").path)))
        XCTAssertFalse(wrote)
        XCTAssertEqual(SearchFolderPick.text(outcome)?.settings, true)
    }

    /// 读得了读不了在子进程里试：在的文件夹读得了，没有的读不了
    func testReadableInAChild() {
        XCTAssertTrue(SearchFolderPick.readable(url("Documents")))
        XCTAssertFalse(SearchFolderPick.readable(url("没有这个")))
    }

    /// 搜索不收（不是它搜的地方）、读不了它的设置：照它说的说
    func testFailures() {
        let refused = SearchFolderPick.add(url("Documents"), saved: { [] }, canRead: { _ in true },
                                           choose: { _ in "螺壳搜索不搜这里" })
        XCTAssertEqual(refused, .failed("螺壳搜索不搜这里"))
        XCTAssertEqual(SearchFolderPick.text(refused)?.text, "没加上：螺壳搜索不搜这里")
        let unreadable = SearchFolderPick.add(url("Documents"), saved: { nil }, canRead: { _ in true },
                                              choose: { _ in XCTFail("不该写"); return nil })
        guard case .failed = unreadable else { return XCTFail("\(unreadable)") }
        XCTAssertNil(SearchFolderPick.text(.added), "加上了不说")
    }
}
