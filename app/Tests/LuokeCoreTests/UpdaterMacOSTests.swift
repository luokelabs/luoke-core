import XCTest
@testable import LuokeCore

/// 检查更新看新版要的 macOS（luoke-core#229）：清单里螺壳本身和引擎包的 min_macos 取高的，这台 Mac 不够就不升
final class UpdaterMacOSTests: XCTestCase {
    private func manifest(hub: String? = nil, engine: String? = nil) -> [String: Any] {
        var items: [String: Any] = [:]
        items["hub"] = hub.map { ["min_macos": $0] } ?? [:]
        items["engine"] = engine.map { ["min_macos": $0] } ?? [:]
        return ["items": items]
    }

    func testRequiredMacOSTakesTheHigherOfHubAndEngine() {
        XCTAssertNil(Updater.requiredMacOS(manifest()))
        XCTAssertNil(Updater.requiredMacOS(manifest(hub: "")))
        XCTAssertEqual(Updater.requiredMacOS(manifest(hub: "26.2")), "26.2")
        XCTAssertEqual(Updater.requiredMacOS(manifest(engine: "26.2")), "26.2")
        XCTAssertEqual(Updater.requiredMacOS(manifest(hub: "26", engine: "26.2")), "26.2")
        XCTAssertEqual(Updater.requiredMacOS(manifest(hub: "27.0", engine: "26.2")), "27.0")
        XCTAssertEqual(Updater.requiredMacOS(manifest(hub: "26.10", engine: "26.9")), "26.10")
    }

    func testThisMacSatisfiesItsOwnVersionButNotAFarFutureOne() {
        XCTAssertTrue(Manifests.macOSSatisfies(Updater.thisMacOS))
        XCTAssertFalse(Manifests.macOSSatisfies("99.0"))
    }

    func testMessageNamesTheReleaseAndBothVersions() {
        let msg = Updater.Failure.needsMacOS(tag: "internal-1119", macOS: "99.0").localizedDescription
        XCTAssertEqual(msg, "internal-1119 需要 macOS 99.0 或更新，这台 Mac 是 macOS \(Updater.thisMacOS)")
    }
}
