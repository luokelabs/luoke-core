import Foundation
import XCTest
@testable import LuokeCore

/// 检查更新核安装包只用系统自带的 codesign：没装命令行工具的 Mac 上 /usr/bin/xcrun 是个壳，调它会失败、弹安装框
final class UpdaterNotarizationTests: XCTestCase {
    func testDMGCheckUsesCodesignNotXcrun() {
        let check = Updater.notarizationCheck
        let args = check.arguments("/tmp/Luoke.dmg")
        XCTAssertEqual(check.tool, "/usr/bin/codesign")
        XCTAssertFalse(([check.tool] + args).contains { $0.contains("xcrun") || $0.contains("stapler") })
        XCTAssertFalse(args.contains("--check-notarization"))       // 不上网
        XCTAssertEqual(args.last, "/tmp/Luoke.dmg")
        let req = args.filter { $0.hasPrefix("-R=") }
        XCTAssertEqual(req.count, 1)                                 // 以 = 开头：当要求文本，不当文件路径
        XCTAssertTrue(req[0].contains("anchor apple generic"))
        XCTAssertTrue(req[0].contains("certificate leaf[subject.OU] = \"\(Updater.teamID)\""))
        XCTAssertTrue(req[0].hasSuffix("and notarized"))
    }

    /// 真跑 codesign：ad-hoc 签的东西过不了这条要求
    func testAdHocSignedCodeFailsTheCheck() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("notarization-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let file = dir.appendingPathComponent("fake")
        try FileManager.default.copyItem(atPath: "/usr/bin/true", toPath: file.path)
        XCTAssertEqual(try run("/usr/bin/codesign", ["-s", "-", "-f", file.path]), 0)
        let check = Updater.notarizationCheck
        XCTAssertNotEqual(try run(check.tool, check.arguments(file.path)), 0)
    }

    private func run(_ tool: String, _ args: [String]) throws -> Int32 {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: tool)
        p.arguments = args
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        try p.run()
        p.waitUntilExit()
        return p.terminationStatus
    }
}
