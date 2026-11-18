import XCTest
@testable import LuokeCore

/// 主窗口左下角那一行（CoreNotice）：按轻重只给最要紧的一件
final class CoreNoticeTests: XCTestCase {
    private func status(_ extra: [String: Any] = [:]) -> HubStatus {
        var o: [String: Any] = ["engine_alive": true, "engine_ready": true, "background": ["state": "running", "reasons": [] as [String]]]
        extra.forEach { o[$0.key] = $0.value }
        return HubStatus(json: o)
    }

    func testNothingToSay() {
        XCTAssertNil(CoreNotice.top(status: status(), unreachable: false, updateFound: false))
    }

    func testOrderErrorPausedDownloadingUpdate() {
        let paused: [String: Any] = ["background": ["state": "paused", "reasons": ["battery"]]]
        let downloading: [String: Any] = ["downloads": ["m": ["state": "downloading", "done": 1, "total": 2]]]
        let all = status(paused.merging(downloading) { a, _ in a }.merging(["breaker": "熔断"]) { a, _ in a })

        XCTAssertEqual(CoreNotice.top(status: all, unreachable: false, updateFound: true)?.kind, .error)
        XCTAssertEqual(CoreNotice.top(status: status(paused.merging(downloading) { a, _ in a }), unreachable: false, updateFound: true)?.kind, .paused)
        XCTAssertEqual(CoreNotice.top(status: status(downloading), unreachable: false, updateFound: true)?.kind, .downloading)
        XCTAssertEqual(CoreNotice.top(status: status(), unreachable: false, updateFound: true), CoreNotice(kind: .update, text: "有新版本", action: .update))
    }

    func testUnreachableIsAnError() {
        XCTAssertEqual(CoreNotice.top(status: nil, unreachable: true, updateFound: false)?.text, "出错了")
        XCTAssertEqual(CoreNotice.top(status: nil, unreachable: false, updateFound: true)?.kind, .update)
    }

    func testPausedSaysWhyAndWhereToChangeIt() {
        func paused(_ reasons: [String]) -> CoreNotice? {
            CoreNotice.top(status: status(["background": ["state": "paused", "reasons": reasons]]), unreachable: false, updateFound: false)
        }
        let battery = paused(["battery"])
        XCTAssertEqual(battery?.text, "已暂停")                        // 左下角那一小块照旧
        XCTAssertTrue(battery?.detail?.contains("「用电池时」") ?? false)   // 设置里那一项的名字照抄
        XCTAssertEqual(battery?.action, .settings)
        XCTAssertNil(battery?.actionLabel)
        let noLarge = paused(["battery_no_large_model"])
        XCTAssertTrue(noLarge?.detail?.contains("不运行大模型") ?? false)
        XCTAssertEqual(noLarge?.action, .settings)
        XCTAssertFalse(battery?.detail?.contains("不运行大模型") ?? true)
        // 用电池时：新主控给 battery_mode，旧主控只给 background_on_battery
        XCTAssertEqual(status(["settings": ["battery_mode": "no_large_model"]]).batteryMode, "no_large_model")
        XCTAssertEqual(status(["settings": ["background_on_battery": true]]).batteryMode, "normal")
        XCTAssertEqual(status().batteryMode, "light")
        XCTAssertEqual(paused(["low_power"])?.action, .batterySettings)
        XCTAssertEqual(paused(["thermal"])?.action, .dismiss)
        XCTAssertEqual(paused(["memory"])?.action, .dismiss)
        XCTAssertEqual(paused(["thermal", "battery"])?.detail, battery?.detail)     // 几个原因：说该先处理的
        XCTAssertNotNil(paused([])?.detail)
        XCTAssertEqual(CoreNotice.top(status: nil, unreachable: true, updateFound: false)?.action, .restart)
        XCTAssertEqual(CoreNotice.top(status: status(), unreachable: false, updateFound: true)?.action, .update)
    }

    func testTextsAreShort() {
        let all = [CoreNotice.top(status: nil, unreachable: true, updateFound: false),
                   CoreNotice.top(status: status(["background": ["state": "paused", "reasons": ["thermal"]]]), unreachable: false, updateFound: false),
                   CoreNotice.top(status: status(["downloads": ["m": ["state": "queued"]]]), unreachable: false, updateFound: false),
                   CoreNotice.top(status: status(), unreachable: false, updateFound: true)]
        for n in all { XCTAssertLessThanOrEqual(n?.text.count ?? 99, 4) }
    }
}
