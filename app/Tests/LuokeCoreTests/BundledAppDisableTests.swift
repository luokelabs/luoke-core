import Foundation
import XCTest
@testable import LuokeCore

/// 「停用」（BundledApps.swift）：先停程序，再记下；记不下就起回来，不能界面说开着、程序却没跑
final class BundledAppDisableTests: XCTestCase {
    private enum Fake: BundledApp {
        static var calls: [String] = []
        static let accessory = "luoke-fake"
        static let manifest: Accessories.Manifest? = nil
        static func start() { calls.append("start") }
        static func stop() { calls.append("stop") }
        static func stopAll(hubApp: URL) {}
    }

    private func disable(record: @escaping () throws -> Void) -> String? {
        Fake.calls = []
        let done = expectation(description: "done")
        var why: String?
        Fake.disable(record: { Fake.calls.append("record"); try record() }) { why = $0; done.fulfill() }
        wait(for: [done], timeout: 5)
        return why
    }

    func testStopsThenRecords() {
        XCTAssertNil(disable {})
        XCTAssertEqual(Fake.calls, ["stop", "record"])
    }

    func testComesBackWhenTheHubDidNotRecordIt() {
        let why = disable { throw HubError(status: 503, code: "busy", message: "螺壳正在启动") }
        XCTAssertEqual(why, "螺壳正在启动")
        XCTAssertEqual(Fake.calls, ["stop", "record", "start"])
    }
}
