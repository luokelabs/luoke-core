import XCTest
@testable import LuokeCore

/// 「应用管理」页给主窗口的数据（AppsSnapshot）：状态、动作和原来行上的按钮、菜单一一对上
final class AppsSnapshotTests: XCTestCase {
    private func row(_ id: String, type: String = "app", installed: String?, latest: String?, size: Int64 = 0,
                     unavailable: String? = nil, bundled: Bool = false, disabled: Bool = false) -> AppsModel.Row {
        AppsModel.Row(id: id, name: id, type: type, installed: installed, latest: latest, size: size, unavailable: unavailable,
                      bundled: bundled, disabled: disabled)
    }

    func testStatesAndActions() {
        let rows = [
            row("luoke-mail", installed: "0.15.0", latest: "0.16.0"),
            row("luoke-ime", type: "input-method", installed: "1.0", latest: "1.0"),
            row("luoke-search", installed: "0.3", latest: nil, bundled: true, disabled: true),
            row("luoke-scan", installed: "0.2", latest: nil, bundled: true),
            row("acme-notes", installed: nil, latest: "1.0", size: 12_000_000),
            row("luoke-x", installed: nil, latest: "1.0", unavailable: "需要 macOS 27 或更新"),
        ]
        let e = AppsSnapshot.entries(rows: rows, phase: .idle, progress: "", fraction: nil, previous: ["luoke-mail": "0.14.2"])
        XCTAssertEqual(e.map(\.state), [.update(to: "0.16.0"), .installed, .disabled, .installed, .installable,
                                        .unavailable(reason: "需要 macOS 27 或更新")])
        XCTAssertEqual(e[0].actions, [.rollback(to: "0.14.2"), .uninstall])
        XCTAssertEqual(e[1].actions, [.restart, .uninstall])
        XCTAssertEqual(e[2].actions, [.enable, .deleteData])                // 随螺壳的：没有卸载
        XCTAssertEqual(e[3].actions, [.disable, .deleteData])
        XCTAssertEqual(e[4].actions, [])
        XCTAssertEqual(e.map(\.poweredBy), [false, false, false, false, true, false])
        XCTAssertEqual(e[0].version, "0.15.0")                               // 装着的版本
        XCTAssertEqual(e[4].version, "1.0")                                  // 没装：清单上的
        XCTAssertEqual(e[0].facts, [])                                       // 装好的不列
        XCTAssertFalse(e[4].facts.isEmpty)
    }

    func testWorkingAndNotice() {
        let rows = [row("luoke-ime", type: "input-method", installed: nil, latest: "1.0")]
        let e = AppsSnapshot.entries(rows: rows, phase: .updating("luoke-ime"), progress: "下载中", fraction: 0.5, previous: [:])
        XCTAssertEqual(e[0].state, .working(label: "下载中", progress: 0.5))
        XCTAssertNil(AppsSnapshot.notice(phase: .idle, imeAdded: false, rows: rows))
        XCTAssertEqual(AppsSnapshot.notice(phase: .done("装好了", true), imeAdded: true, rows: rows),
                       AppsNoticeEntry(text: "装好了", ok: true, keyboardSettingsFor: "luoke-ime"))
        XCTAssertNil(AppsSnapshot.notice(phase: .done("没装上", false), imeAdded: false, rows: rows)?.keyboardSettingsFor)
    }
}
