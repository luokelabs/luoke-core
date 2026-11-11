import ServiceManagement
import XCTest
@testable import LuokeCore

/// 每登记一次登录项 macOS 就弹一次「已添加登录项」：升级后只在登记找不到了、或螺壳换了位置时重新登记（#130）
final class LoginItemTests: XCTestCase {
    private let here = "/Applications/螺壳.app"

    func testAnEnabledItemAtThisCopyIsLeftAlone() {
        XCTAssertFalse(LoginItem.needsRegister(status: .enabled, savedPath: here, currentPath: here))
        // 记位置之前的版本登记的：系统里那条就是这一份，不重新登记（repoint 只把位置记下）
        XCTAssertFalse(LoginItem.needsRegister(status: .enabled, savedPath: nil, currentPath: here))
    }

    func testAMovedCopyIsRegisteredAgain() {
        XCTAssertTrue(LoginItem.needsRegister(status: .enabled, savedPath: "/Applications/螺壳主控.app", currentPath: here))
        XCTAssertTrue(LoginItem.needsRegister(status: .enabled, savedPath: NSHomeDirectory() + "/Applications/螺壳.app", currentPath: here))
    }

    func testAMissingItemIsRegisteredAgain() {
        XCTAssertTrue(LoginItem.needsRegister(status: .notFound, savedPath: here, currentPath: here))
        XCTAssertTrue(LoginItem.needsRegister(status: .notFound, savedPath: nil, currentPath: here))
    }

    func testTheUsersChoiceIsKept() {
        // 用户关掉过、或者等着在系统设置里允许：都不替用户登记
        for saved in [here, nil, "/elsewhere/螺壳.app"] {
            XCTAssertFalse(LoginItem.needsRegister(status: .notRegistered, savedPath: saved, currentPath: here))
            XCTAssertFalse(LoginItem.needsRegister(status: .requiresApproval, savedPath: saved, currentPath: here))
        }
    }

    /// 只有 LoginItem 自己登记：别处直接调 SMAppService.mainApp.register() 的话，升级时又会每次都弹
    func testOnlyLoginItemRegisters() throws {
        let sources = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("Sources/LuokeCore")
        let files = try FileManager.default.contentsOfDirectory(at: sources, includingPropertiesForKeys: nil).filter { $0.pathExtension == "swift" }
        XCTAssertFalse(files.isEmpty, sources.path)
        for f in files where f.lastPathComponent != "LoginItem.swift" {
            let text = try String(contentsOf: f, encoding: .utf8)
            XCTAssertFalse(text.contains("mainApp.register("), "\(f.lastPathComponent) 直接登记了登录项，改用 LoginItem.register()")
        }
    }
}
