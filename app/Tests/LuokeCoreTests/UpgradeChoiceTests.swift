import Foundation
import XCTest
@testable import LuokeCore

/// 升级上来以后搜索、扫描怎么办（BundledApps.upgradeChoice）：用过的记成开着，用户停用过的照旧关着，没用过的补问一次
final class UpgradeChoiceTests: XCTestCase {
    private var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("upgrade-choice-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    private func choice(_ state: [String: Any]?, hadOld: Bool, leftData: Bool = false, offered: Bool = false, firstRunNeeded: Bool = false) throws -> BundledApps.UpgradeChoice {
        let file = dir.appendingPathComponent("state.json")
        try? FileManager.default.removeItem(at: file)
        if let state { try JSONSerialization.data(withJSONObject: state).write(to: file) }
        return BundledApps.upgradeChoice(hadOld: hadOld, leftData: leftData, recorded: BundledApps.recorded("luoke-scan", state: file),
                                         offered: offered, firstRunNeeded: firstRunNeeded)
    }

    private func manifest(_ name: String, data: [String]) throws -> URL {
        let resources = dir.appendingPathComponent("Resources")
        let folder = resources.appendingPathComponent(name)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        try JSONSerialization.data(withJSONObject: ["accessory": "luoke-" + name, "data": data]).write(to: folder.appendingPathComponent("luoke-accessory.json"))
        return resources
    }

    func testOldAppWithoutARecordIsKeptOn() throws {
        XCTAssertEqual(try choice(nil, hadOld: true), .keepOn)                                          // 没有状态文件
        XCTAssertEqual(try choice(["enabled": ["luoke-search": true]], hadOld: true), .keepOn)         // 别的应用有记录
        XCTAssertEqual(try choice(["enabled": [String: Bool]()], hadOld: true, offered: true), .keepOn)
    }

    func testTheUsersOwnChoiceStays() throws {
        XCTAssertEqual(try choice(["enabled": ["luoke-scan": false]], hadOld: true), .none)            // 停用过的不覆盖
        XCTAssertEqual(try choice(["enabled": ["luoke-scan": false]], hadOld: false), .none)
        XCTAssertEqual(try choice(["enabled": ["luoke-scan": true]], hadOld: true), .none)
    }

    func testNeverUsedIsOfferedOnce() throws {
        XCTAssertEqual(try choice(nil, hadOld: false), .offer)
        XCTAssertEqual(try choice(nil, hadOld: false, offered: true), .none)
        XCTAssertEqual(try choice(nil, hadOld: false, firstRunNeeded: true), .none)                    // 首次安装自己问
    }

    /// 旧的 .app 在更早的一次升级里已经挪走了（比如装过 rc3 的机器）：数据目录还在、没记录，也算用过
    func testLeftDataWithoutARecordIsKeptOn() throws {
        XCTAssertEqual(try choice(nil, hadOld: false, leftData: true), .keepOn)
        XCTAssertEqual(try choice(["enabled": ["luoke-scan": false]], hadOld: false, leftData: true), .none)   // 停用过的不覆盖
        XCTAssertEqual(try choice(nil, hadOld: false, leftData: true, offered: true), .none)                   // 问过了：用户的回答算数
        XCTAssertEqual(try choice(nil, hadOld: false, leftData: true, firstRunNeeded: true), .none)            // 首次安装自己问
    }

    func testLeftDataLooksAtTheManifestsDataFolders() throws {
        let home = dir.appendingPathComponent("home").path
        let resources = try manifest("scan", data: ["~/.luoke-scan"])
        XCTAssertFalse(BundledApps.leftData("luoke-scan", resources: resources, home: home))
        try FileManager.default.createDirectory(atPath: home + "/.luoke-scan", withIntermediateDirectories: true)
        XCTAssertTrue(BundledApps.leftData("luoke-scan", resources: resources, home: home))
        XCTAssertFalse(BundledApps.leftData("luoke-search", resources: resources, home: home))                // 没有清单
    }

    func testLeftDataIgnoresTheWholeHomeFolder() throws {
        let home = dir.appendingPathComponent("home").path
        try FileManager.default.createDirectory(atPath: home, withIntermediateDirectories: true)
        let resources = try manifest("scan", data: ["~", "~/", "relative/path"])
        XCTAssertFalse(BundledApps.leftData("luoke-scan", resources: resources, home: home))
    }

    /// 螺壳搜索记成开着、没有完全磁盘访问：先不起，等用户点「开启」；有了完全磁盘访问就放开
    func testSearchWaitsForReallowWithoutFullDisk() throws {
        let d = try XCTUnwrap(UserDefaults(suiteName: "reallow-\(UUID().uuidString)"))
        XCTAssertFalse(SearchHelper.Reallow.holds(defaults: d, fullDisk: { false }))       // 没在等

        SearchHelper.Reallow.holdIfNeeded(defaults: d, fullDisk: true)
        XCTAssertFalse(SearchHelper.Reallow.waiting(defaults: d))                          // 有完全磁盘访问：不用等

        SearchHelper.Reallow.holdIfNeeded(defaults: d, fullDisk: nil)                      // 看不出来：当没有
        XCTAssertTrue(SearchHelper.Reallow.holds(defaults: d, fullDisk: { nil }))
        SearchHelper.Reallow.holdIfNeeded(defaults: d, fullDisk: false)
        XCTAssertTrue(SearchHelper.Reallow.holds(defaults: d, fullDisk: { false }))
        XCTAssertTrue(SearchHelper.Reallow.waiting(defaults: d))                           // 下次打开还在等

        XCTAssertFalse(SearchHelper.Reallow.holds(defaults: d, fullDisk: { true }))        // 后来给了完全磁盘访问
        XCTAssertFalse(SearchHelper.Reallow.waiting(defaults: d))
    }

    func testReallowClearsWhenTheUserAllows() throws {
        let d = try XCTUnwrap(UserDefaults(suiteName: "reallow-\(UUID().uuidString)"))
        SearchHelper.Reallow.holdIfNeeded(defaults: d, fullDisk: false)
        SearchHelper.Reallow.set(false, defaults: d)                                       // 「开启」窗口问完
        XCTAssertFalse(SearchHelper.Reallow.holds(defaults: d, fullDisk: { false }))
        XCTAssertNil(d.object(forKey: SearchHelper.Reallow.key))
    }

    func testRecordedReadsTheEnabledMap() throws {
        let file = dir.appendingPathComponent("state.json")
        XCTAssertNil(BundledApps.recorded("luoke-scan", state: file))
        try JSONSerialization.data(withJSONObject: ["enabled": ["luoke-scan": false]]).write(to: file)
        XCTAssertEqual(BundledApps.recorded("luoke-scan", state: file), false)
        XCTAssertNil(BundledApps.recorded("luoke-search", state: file))
    }

    func testOfferedIsRememberedPerApp() throws {
        let d = try XCTUnwrap(UserDefaults(suiteName: "upgrade-choice-\(UUID().uuidString)"))
        XCTAssertFalse(HelperOffer.offered("luoke-scan", defaults: d))
        HelperOffer.markOffered(["luoke-scan"], defaults: d)
        HelperOffer.markOffered(["luoke-scan", "luoke-search"], defaults: d)
        XCTAssertTrue(HelperOffer.offered("luoke-scan", defaults: d))
        XCTAssertEqual(d.stringArray(forKey: HelperOffer.offeredKey), ["luoke-scan", "luoke-search"])
    }
}
