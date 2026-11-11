import Darwin
import Foundation
import XCTest
@testable import LuokeCore

/// 起子进程（Spawn.swift）：默认 disclaim、--disclaim-exec 跳板，和「只在 Spawn.swift 里起进程」的规矩。
/// 只起 /bin/sleep、/usr/bin/true 这类系统程序，不碰本机用户的数据。授权本身（完全磁盘访问）测不了，要正式签名的包，见 PR 说明
final class SpawnTests: XCTestCase {
    private typealias Responsible = @convention(c) (pid_t) -> pid_t
    private let responsible: Responsible = unsafeBitCast(
        dlsym(UnsafeMutableRawPointer(bitPattern: -2), "responsibility_get_pid_responsible_for_pid"), to: Responsible.self)

    private func reap(_ pid: pid_t) {
        kill(pid, SIGKILL)
        var status: Int32 = 0
        while waitpid(pid, &status, 0) < 0 && errno == EINTR {}
    }

    // MARK: posix_spawn 这条

    func testDisclaimedChildIsItsOwnResponsibleProcess() throws {
        let pid = try Spawn.run("/bin/sleep", ["30"], env: [], cwd: "/", suspended: true, fds: [])
        defer { reap(pid) }
        XCTAssertEqual(responsible(pid), pid)
    }

    func testLuokeAccessKeepsTheParentsResponsibleProcess() throws {
        let pid = try Spawn.run("/bin/sleep", ["30"], env: [], cwd: "/", suspended: true, fds: [], access: .luoke)
        defer { reap(pid) }
        XCTAssertEqual(responsible(pid), responsible(getpid()))
        XCTAssertNotEqual(responsible(pid), pid)
    }

    func testCaptureDisclaimsByDefault() throws {
        let out = try Spawn.capture("/bin/sh", ["-c", "echo ok"], env: [], capture: 1) { _ in nil }
        XCTAssertEqual(String(decoding: out.data, as: UTF8.self), "ok\n")
    }

    // MARK: Process 这条：经跳板

    func testProcessGoesThroughTheTrampolineUnlessLuoke() {
        let me = URL(fileURLWithPath: "/Applications/X.app/Contents/MacOS/X")
        let d = Spawn.process("/usr/bin/defaults", ["export", "x", "-"], trampoline: me)
        XCTAssertEqual(d.executableURL, me)
        XCTAssertEqual(d.arguments, [Spawn.disclaimFlag, "/usr/bin/defaults", "export", "x", "-"])
        let l = Spawn.process("/bin/sh", ["-c", "true"], access: .luoke, trampoline: me)
        XCTAssertEqual(l.executableURL?.path, "/bin/sh")
        XCTAssertEqual(l.arguments, ["-c", "true"])
        let dev = Spawn.process("/usr/bin/true", [], trampoline: nil)      // 没打包成 .app：直接起
        XCTAssertEqual(dev.executableURL?.path, "/usr/bin/true")
    }

    /// 编出来的本体（swift build 的 LuokeCore）当跳板：换成目标程序，pid 不变，自己负责
    private var builtApp: URL? {
        let dir = Bundle(for: SpawnTests.self).bundleURL.deletingLastPathComponent()
        let exe = dir.appendingPathComponent("LuokeCore")
        return FileManager.default.isExecutableFile(atPath: exe.path) ? exe : nil
    }

    func testTrampolineExecsDisclaimedInPlace() throws {
        let app = try XCTUnwrap(builtApp, "没找到编出来的 LuokeCore")
        let p = Spawn.process("/bin/sleep", ["30"], trampoline: app)
        p.standardInput = FileHandle.nullDevice
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        try p.run()
        defer { p.terminate(); p.waitUntilExit() }
        let pid = p.processIdentifier
        var path = ""
        let deadline = Date().addingTimeInterval(10)
        while Date() < deadline {
            var b = [CChar](repeating: 0, count: 4096)
            if proc_pidpath(pid, &b, 4096) > 0 { path = String(cString: b) }
            if path == "/bin/sleep" { break }
            Thread.sleep(forTimeInterval: 0.05)
        }
        XCTAssertEqual(path, "/bin/sleep")
        XCTAssertEqual(responsible(pid), pid)
    }

    func testTrampolineRefusesRelativePaths() throws {
        let app = try XCTUnwrap(builtApp, "没找到编出来的 LuokeCore")
        let p = Process()       // 测跳板自己，故意不经 Spawn.process
        p.executableURL = app
        p.arguments = [Spawn.disclaimFlag, "sleep", "1"]
        p.standardError = FileHandle.nullDevice
        try p.run()
        p.waitUntilExit()
        XCTAssertEqual(p.terminationStatus, Spawn.disclaimFailed)
    }

    // MARK: 规矩：只在 Spawn.swift 里起进程，.luoke 只在登记过的几处

    /// 用 .luoke 的地方，和为什么（Spawn.swift 开头）。加一处要在这里登记
    static let luokeAllowed: Set<String> = [
        "SearchHelper.swift",   // 螺壳搜索：读用户的文件
        "ToolHost.swift",       // 螺壳扫描（agent 用的）
        "ToolLauncher.swift",   // 螺壳扫描（空闲任务）
        "ScanElevate.swift",    // 替扫描删受保护的东西
        "Swap.swift",           // 换包：改螺壳.app
        "AppPlacement.swift",   // 拷进「应用程序」、去下载标记：改 .app
        "Updater.swift",        // 升级
        "Spawn.swift", "HelperProcess.swift", "Installer.swift",     // 定义和转交 access 的地方
    ]

    func testSpawnRules() throws {
        let sources = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("Sources/LuokeCore")
        let files = try FileManager.default.contentsOfDirectory(atPath: sources.path).filter { $0.hasSuffix(".swift") }
        XCTAssertTrue(files.contains("Spawn.swift"))
        for f in files {
            let code = try String(contentsOf: sources.appendingPathComponent(f), encoding: .utf8)
                .replacingOccurrences(of: "(?<!:)//.*", with: "", options: .regularExpression)    // 去掉注释，不碰字符串里的 https://
            if f != "Spawn.swift" {
                XCTAssertNil(code.range(of: "(?<![A-Za-z_])Process\\(\\)", options: .regularExpression), "\(f)：起进程要经 Spawn.process")
                XCTAssertFalse(code.contains("posix_spawn("), "\(f)：起进程要经 Spawn.run")
                XCTAssertFalse(code.contains("executableURL ="), "\(f)：起进程要经 Spawn.process")
            }
            if !Self.luokeAllowed.contains(f) {
                XCTAssertFalse(code.contains("access: .luoke") || code.contains("access = .luoke"), "\(f)：用 .luoke 要在 SpawnTests.luokeAllowed 登记")
            }
        }
    }
}
