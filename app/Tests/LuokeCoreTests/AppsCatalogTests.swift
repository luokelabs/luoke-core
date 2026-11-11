import XCTest
@testable import LuokeCore

/// 「添加应用」页（对照稿）：三段、卡片上的介绍和要什么、详情里的几样从哪来，装的时候走到哪一步
final class AppsCatalogTests: XCTestCase {
    private let recipe: [String: Any] = [
        "tiers": ["16": ["min_gb": 0, "max_gb": 24], "32": ["min_gb": 24, "max_gb": 64], "128": ["min_gb": 64, "max_gb": 4096]],
        "tasks": ["x-ime.rerank": ["accessory": "x-ime", "tiers": ["32", "128"]],
                  "x-ime.predict": ["accessory": "x-ime", "tiers": ["128"]],
                  "x-big.explain": ["accessory": "x-big", "tiers": ["128"]],
                  "x-unverified.task": ["accessory": "x-unverified"]],
        "verified": ["x-ime": ["versions": ["1.0"]], "x-big": ["versions": ["1.0"]], "x-unverified": ["versions": []]],
    ]

    private func row(installed: String? = nil, latest: String? = "1.0", type: String = "app") -> AppsModel.Row {
        AppsModel.Row(id: "x-ime", name: "示例", type: type, installed: installed, latest: latest, size: 1, unavailable: nil)
    }

    /// 没装：介绍、会要什么、读不读写记忆从发布清单的 listing 来；会主动找你、会代你做、数据发布清单不抄，是「不知道」不是「没有」
    func testNotInstalledReadsTheReleaseListing() {
        var r = row()
        let item: [String: Any] = ["memory": true, "listing": ["tagline": "一句话", "category": "输入",
                                                               "permissions": [["id": "mic", "kind": "system", "title": "麦克风", "facts": ["只在按住时听"]],
                                                                               ["id": "bad", "kind": "system"]]]]
        AppsModel.describe(&r, manifest: nil, item: item, recipe: recipe)
        XCTAssertEqual(r.tagline, "一句话")
        XCTAssertEqual(r.needs, [AppsModel.Need(title: "麦克风", kind: "system", facts: ["只在按住时听"])], "没有 title 的不列")
        XCTAssertTrue(r.memory)
        XCTAssertNil(r.reports)
        XCTAssertNil(r.actions)
        XCTAssertEqual(r.data, [])
        XCTAssertEqual(r.minTier, "32", "它的任务开着的最低一档")
    }

    /// 装着：照应用自己的清单，会主动找你是 reports 的 title，会代你做只算会改东西的动作
    func testInstalledReadsItsOwnManifest() {
        var r = row(installed: "1.0")
        let manifest: [String: Any] = [
            "memory": false, "data": ["~/.x"],
            "capabilities": ["listing": ["tagline": "装着的介绍"],
                             "reports": [["kind": "a", "title": "要你处理的"], ["kind": "b"]],
                             "actions": [["name": "do", "title": "照你点的做", "writes": true], ["name": "look", "title": "只是看看"]]],
        ]
        AppsModel.describe(&r, manifest: manifest, item: ["listing": ["tagline": "发布清单的介绍"], "memory": true], recipe: recipe)
        XCTAssertEqual(r.tagline, "装着的介绍")
        XCTAssertEqual(r.reports, ["要你处理的"])
        XCTAssertEqual(r.actions, ["照你点的做"])
        XCTAssertEqual(r.data, ["~/.x"])
        XCTAssertFalse(r.memory, "装着的清单说了算")
        XCTAssertEqual(r.needs, [])
    }

    /// 「会要」是你给它的：系统权限（`kind: "system"`，或自己的清单写了 tcc）、要你连的账号、每次都要你点的事；
    /// 「会代你做」是交给它自己做的（`delegate`）。不认得的种类当作要你给的
    func testWhichPermissionsAreNeedsAndWhichItDoes() {
        var r = row(installed: "1.0")
        let manifest: [String: Any] = ["capabilities": ["permissions": [
            ["id": "fda", "kind": "system", "tcc": "SystemPolicyAllFiles", "title": "完全磁盘访问"],
            ["id": "auto", "kind": "delegate", "title": "不用点就清 4 类"],
            ["id": "admin", "kind": "each-time", "title": "删除系统目录里的东西（要 Mac 的密码）"],
            ["id": "mailbox", "kind": "account", "title": "邮箱"],
            ["id": "folders", "kind": "each-time", "title": "读你选的文件夹"],
            ["id": "mic", "tcc": "Microphone", "title": "麦克风"],
            ["id": "odd", "kind": "later", "title": "以后的种类"],
        ]]]
        AppsModel.describe(&r, manifest: manifest, item: nil, recipe: nil)
        XCTAssertEqual(r.needs.filter { !$0.delegated }.map(\.title),
                       ["完全磁盘访问", "删除系统目录里的东西（要 Mac 的密码）", "邮箱", "读你选的文件夹", "麦克风", "以后的种类"])
        XCTAssertEqual(r.needs.filter(\.delegated).map(\.title), ["不用点就清 4 类"])
        XCTAssertEqual(r.needs.filter(\.system).map(\.title), ["完全磁盘访问", "麦克风"], "带齿轮的只有系统权限")
        XCTAssertTrue(AppsModel.Need(title: "照片图库", kind: "system", facts: []).system, "发布清单只抄 kind，不抄 tcc")
    }

    func testMinTier() {
        XCTAssertEqual(AppsModel.minTier("x-big", recipe: recipe), "128")
        XCTAssertNil(AppsModel.minTier("x-unverified", recipe: recipe), "没有认过的版本：任务不算开着")
        XCTAssertNil(AppsModel.minTier("x-none", recipe: recipe), "配方里没有它的任务")
        XCTAssertNil(AppsModel.minTier("x-ime", recipe: nil))
    }

    func testSections() {
        XCTAssertEqual(row().section, .install)
        XCTAssertEqual(row(installed: "0.9").section, .update)
        XCTAssertEqual(row(installed: "1.0").section, .installed)
        XCTAssertEqual(row(installed: "1.0", latest: nil).section, .installed, "本体里带的：没有发布清单的版本")
        XCTAssertEqual(AppsModel.Section.allCases.map(\.title), ["有更新", "可以装", "装好了"])
    }

    /// 装的几步：下载；字节齐了、主控还没说装好，是在核对签名、装到位。别的应用不在装
    func testStepFollowsTheDownload() {
        let m = AppsModel()
        let r = row(type: "input-method")
        XCTAssertEqual(r.steps, ["下载", "核对签名", "装进「输入法」"])
        XCTAssertEqual(row().steps.last, "装进「应用程序」")
        XCTAssertNil(m.step(r))
        m.phase = .updating("x-ime")
        XCTAssertEqual(m.step(r), 0)
        m.fraction = 0.4
        XCTAssertEqual(m.step(r), 0)
        m.fraction = 1
        XCTAssertEqual(m.step(r), 1)
        m.phase = .updating("other")
        XCTAssertNil(m.step(r))
        XCTAssertTrue(m.busy)
    }
}
