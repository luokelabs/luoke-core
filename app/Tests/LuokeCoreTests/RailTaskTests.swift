import XCTest
@testable import LuokeCore

/// 右边栏的任务队列（RailTask）：主控 /status 的 queue 换成给人看的样子
final class RailTaskTests: XCTestCase {
    func testRunningAndQueuedRows() {
        let st = HubStatus(json: ["queue": [
            "running": [
                ["id": "request:luoke-mail:mail.triage", "tool": "luoke-mail", "name": "mail.triage", "kind": "request", "fraction": 0.4],
                ["id": "luoke-mem.learn", "tool": "luoke-mem", "name": "learn", "title": "整理记忆", "kind": "job"],
                ["id": "endpoint", "name": "gemma-4-26b-a4b-it-8bit", "kind": "endpoint"],
            ],
            "queued": [
                ["id": "luoke-mail.clean", "tool": "luoke-mail", "name": "clean", "wait": ["kind": "time", "at": 1_800_000_000]],
                ["id": "x.y", "tool": "x", "name": "y", "wait": ["kind": "power"]],
            ],
        ] as [String: Any], "output": ["today": 1234, "total": 184_000, "since": "2026-11-12"]])
        let rows = RailTask.rows(st)
        // 右边栏每行下面已经写了应用名：名字里不再带它
        XCTAssertEqual(rows.map(\.title), ["在用模型", "整理记忆", "本机接口 · " + ModelName.display("gemma-4-26b-a4b-it-8bit"),
                                           "后台任务", "后台任务"])
        XCTAssertEqual(rows.map(\.tool), ["luoke-mail", "luoke-mem", nil, "luoke-mail", "x"])
        XCTAssertEqual(rows[0].state, .running(fraction: 0.4))
        XCTAssertEqual(rows[1].state, .running(fraction: nil))
        XCTAssertNil(rows[2].tool)
        XCTAssertEqual(rows[3].state, .queued(.time(Date(timeIntervalSince1970: 1_800_000_000))))
        XCTAssertEqual(rows[4].state, .queued(.power))
        XCTAssertEqual(st.tokensToday, 1234)
        XCTAssertEqual(st.tokensTotal, 184_000)
        XCTAssertEqual(st.tokensSince, "2026-11-12")
    }

    func testEmpty() {
        XCTAssertEqual(RailTask.rows(nil), [])
        XCTAssertEqual(RailTask.rows(HubStatus(json: [:])), [])
        XCTAssertNil(HubStatus(json: [:]).tokensTotal)
    }

    func testSince() {
        let now = DateComponents(calendar: Calendar.current, year: 2026, month: 12, day: 1).date!
        XCTAssertEqual(RailTask.since("2026-11-12", now: now), "11 月 12 日")
        XCTAssertEqual(RailTask.since("2025-11-12", now: now), "2025 年 11 月 12 日")
        XCTAssertNil(RailTask.since(nil, now: now))
    }
}
