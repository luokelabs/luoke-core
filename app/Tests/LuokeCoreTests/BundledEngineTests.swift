import Foundation
import XCTest
@testable import LuokeCore

/// 打进螺壳.app 的螺壳引擎（Contents/Helpers/luoke-engine）：配方要的正是这一版才算装好（和主控 hub.py 的 bundled_engine 同一规则）
final class BundledEngineTests: XCTestCase {
    func testBundledEngineNeedsTheProgramAndItsVersion() throws {
        let app = FileManager.default.temporaryDirectory.appendingPathComponent("engine-\(UUID().uuidString).app", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: app) }
        let exe = app.appendingPathComponent("Contents/Helpers/luoke-engine")
        XCTAssertNil(Manifests.bundledEngine(app: app, info: ["LuokeEngineVersion": "0.1.0"]))       // 没打进来
        try FileManager.default.createDirectory(at: exe.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("#!/bin/sh\n".utf8).write(to: exe)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: exe.path)
        XCTAssertNil(Manifests.bundledEngine(app: app, info: [:]))                                  // 没记版本
        let b = try XCTUnwrap(Manifests.bundledEngine(app: app, info: ["LuokeEngineVersion": "0.1.0"]))
        XCTAssertEqual(b.path, exe.path)
        XCTAssertEqual(b.version, "0.1.0")
        XCTAssertTrue(Manifests.bundledEngineMatches("luoke-engine-0.1.0", bundled: b))
        XCTAssertFalse(Manifests.bundledEngineMatches("luoke-engine-0.2.0", bundled: b))
        XCTAssertFalse(Manifests.bundledEngineMatches("omlx-0.7.2", bundled: b))                   // 配方还是 oMLX：照旧下引擎包
        XCTAssertFalse(Manifests.bundledEngineMatches("luoke-engine-0.1.0", bundled: nil))
    }
}
