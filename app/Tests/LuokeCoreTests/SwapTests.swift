import XCTest
@testable import LuokeCore

/// 换包（Swap.swift）。beta 2 安全审查 M1：标准账户升级时 root 跑的是用户目录里的 run/swap.sh、换上的是用户目录里的暂存版，
/// 同账户的程序在等旧版退出的那两分钟里改掉它们就拿到了 root。这里用合成的、临时签名的小 .app 真跑换包命令（以当前用户跑：
/// 带临时目录就走 root 那条路——拷进临时目录、核签名、只动那一份），lsregister 和 open 换成 echo，不碰装着的螺壳。
final class SwapTests: XCTestCase {
    private var dir: URL!
    private let id = "ai.luoke.swaptest"

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("swap-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    // MARK: 工具

    @discardableResult
    private func run(_ path: String, _ args: [String]) throws -> (Int32, String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: path)
        p.arguments = args
        p.environment = ["PATH": Swap.pathValue]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = out
        try p.run()
        let text = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        p.waitUntilExit()
        return (p.terminationStatus, text)
    }

    /// 一个能签名的最小 .app：可执行文件是 /usr/bin/true 的一份拷贝，Resources/marker 记是哪一版
    private func makeApp(_ url: URL, marker: String, identifier: String? = nil) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: url.appendingPathComponent("Contents/MacOS"), withIntermediateDirectories: true)
        try fm.createDirectory(at: url.appendingPathComponent("Contents/Resources"), withIntermediateDirectories: true)
        try fm.copyItem(atPath: "/usr/bin/true", toPath: url.appendingPathComponent("Contents/MacOS/t").path)
        let plist: [String: Any] = ["CFBundleIdentifier": identifier ?? id, "CFBundleExecutable": "t", "CFBundlePackageType": "APPL",
                                    "CFBundleName": "t"]
        try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
            .write(to: url.appendingPathComponent("Contents/Info.plist"))
        try marker.write(to: url.appendingPathComponent("Contents/Resources/marker"), atomically: true, encoding: .utf8)
        let (rc, out) = try run("/usr/bin/codesign", ["--force", "--sign", "-", url.path])
        XCTAssertEqual(rc, 0, out)
    }

    private func marker(_ app: URL) -> String? {
        try? String(contentsOf: app.appendingPathComponent("Contents/Resources/marker"), encoding: .utf8)
    }

    /// 测试用的脚本：lsregister、open 换成 echo（只在测试里换，正式的脚本不留开关）
    private var testScript: String {
        var s = Swap.script
        let ls = "LS=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
        XCTAssertTrue(s.contains(ls) && s.contains("OPEN=/usr/bin/open"))
        s = s.replacingOccurrences(of: ls, with: "LS=echo").replacingOccurrences(of: "OPEN=/usr/bin/open", with: "OPEN=echo")
        return s
    }

    private struct Layout {
        let apps, home, staged, target, prev, result, log: URL
    }

    /// apps 当「应用程序」，home 当环境目录（暂存处、previous.noindex、结果、日志都在这里）
    private func layout() throws -> Layout {
        let apps = dir.appendingPathComponent("Applications"), home = dir.appendingPathComponent("env")
        for d in [apps, home.appendingPathComponent("staging.noindex"), home.appendingPathComponent("previous.noindex"),
                  home.appendingPathComponent("run"), home.appendingPathComponent("logs")] {
            try FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        }
        let log = home.appendingPathComponent("logs/swap.log")
        FileManager.default.createFile(atPath: log.path, contents: nil)
        return Layout(apps: apps, home: home, staged: home.appendingPathComponent("staging.noindex/螺壳.app"),
                      target: apps.appendingPathComponent("螺壳.app"), prev: home.appendingPathComponent("previous.noindex/螺壳.app"),
                      result: home.appendingPathComponent("run/swap-result.json"), log: log)
    }

    /// 已经退出的进程号：脚本不用等
    private func deadPID() throws -> pid_t {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/true")
        try p.run()
        p.waitUntilExit()
        return p.processIdentifier
    }

    /// 起换包命令（和 Swap.start 交给 sh 的一样，只换了 lsregister、open），等它在后台做完：结果写了、日志交回了
    private func swap(_ l: Layout, current: URL?, admin: Bool, requirement: String? = nil) throws -> (ok: Bool, why: String, log: String) {
        let cmd = Swap.command(admin: admin, waitPID: try deadPID(), current: current, staged: l.staged, target: l.target, previous: l.prev,
                               result: l.result, uid: getuid(), requirement: requirement ?? "=identifier \"\(id)\"", log: l.log, script: testScript)
        let (rc, out) = try run("/bin/sh", ["-c", cmd])
        XCTAssertEqual(rc, 0, out)
        var log = ""
        let deadline = Date().addingTimeInterval(60)
        while Date() < deadline {
            log = (try? String(contentsOf: l.log, encoding: .utf8)) ?? ""
            if FileManager.default.fileExists(atPath: l.result.path), log.contains("== "),
               !admin || !FileManager.default.fileExists(atPath: workDir(log) ?? "/nonexistent") && log.contains("work=") { break }
            Thread.sleep(forTimeInterval: 0.2)
        }
        Thread.sleep(forTimeInterval: 0.3)
        log = (try? String(contentsOf: l.log, encoding: .utf8)) ?? ""
        let d = try Data(contentsOf: l.result)
        let o = try XCTUnwrap(JSONSerialization.jsonObject(with: d) as? [String: Any])
        return (o["ok"] as? Bool ?? false, o["why"] as? String ?? "", log)
    }

    private func workDir(_ log: String) -> String? {
        guard let r = log.range(of: "work=") else { return nil }
        let rest = log[r.upperBound...].prefix { $0 != "\n" }
        return rest.isEmpty ? nil : String(rest)
    }

    // MARK: 命令本身

    func testRootCommandRunsNothingFromTheUsersFolders() {
        let env = URL(fileURLWithPath: "/Users/x/Library/Application Support/luoke-core")
        let cmd = Swap.command(admin: true, waitPID: 123, current: URL(fileURLWithPath: "/Applications/螺壳.app"),
                               staged: env.appendingPathComponent("staging.noindex/螺壳.app"), target: URL(fileURLWithPath: "/Applications/螺壳.app"),
                               previous: env.appendingPathComponent("previous.noindex/螺壳.app"), result: env.appendingPathComponent("run/swap-result.json"),
                               uid: 501, requirement: "=identifier \"ai.luoke.core\"", log: env.appendingPathComponent("logs/swap.log"))
        // 先清以前没起来的、root 的、超过一天的临时目录，再建只有 root 能进的临时目录；脚本整段在命令里（sh -c），不读任何文件
        let lines = cmd.components(separatedBy: "\n")
        XCTAssertEqual(lines[0], "umask 077")
        XCTAssertEqual(lines[1], "/usr/bin/find /private/var/tmp -maxdepth 1 -type d -user 0 -name 'luoke-swap.????????' -mmin +1440 -exec /bin/rm -rf {} + 2>/dev/null")
        XCTAssertEqual(lines[2], "d=$(/usr/bin/mktemp -d /private/var/tmp/luoke-swap.XXXXXXXX) || exit 70")
        XCTAssertTrue(lines[3].hasPrefix("/bin/sh -c '"), lines[3])
        XCTAssertFalse(cmd.contains("swap.sh"))
        // 不用 nohup：macOS 27 上授权框起的 root 进程里它报 can't detach from console，脚本一行不跑（脚本里的注释不算）
        let outside = cmd.replacingOccurrences(of: Swap.quote(Swap.script), with: "SCRIPT")
        XCTAssertFalse(outside.contains("nohup"), outside)
        XCTAssertTrue(Swap.script.contains("trap '' HUP"))
        // root 的输出只写进那个临时目录，不往用户的日志（用户能换成链接）里写；等脚本报「起来了」，没等到就报错
        XCTAssertTrue(outside.contains(" SCRIPT luoke-swap "), outside)
        XCTAssertTrue(outside.contains(" \"$d\" '\(env.path)/logs/swap.log' </dev/null >\"$d/swap.log\" 2>&1 &\n"), outside)
        XCTAssertFalse(cmd.contains(">> '"), cmd)                  // 脚本外没有往用户路径的重定向
        XCTAssertTrue(outside.hasSuffix("/bin/rm -rf \"$d\"; exit 71; fi"), outside)
        // 换上的是拷进临时目录、核过签名的那一份；结果、日志以用户身份写；不再 chown
        XCTAssertTrue(Swap.script.contains(#"/usr/bin/codesign --verify --deep --strict -R "$REQ" "$NEW""#))
        XCTAssertTrue(Swap.script.contains(#"asuser /bin/sh -c 'cat > "$1"' say "$RESULT""#))
        XCTAssertTrue(Swap.script.contains(#"asuser /bin/sh -c 'cat >> "$1"' log "$LOG" < "$WORK/swap.log""#))
        XCTAssertFalse(Swap.script.contains("chown"))
        // 以用户身份跑的：直接追加到日志，也不用 nohup
        let user = Swap.command(admin: false, waitPID: 123, current: nil, staged: env, target: env, previous: env, result: env, uid: 501,
                                requirement: "=x", log: env.appendingPathComponent("logs/swap.log"))
        XCTAssertTrue(user.hasPrefix("/bin/sh -c '"))
        XCTAssertFalse(user.replacingOccurrences(of: Swap.quote(Swap.script), with: "SCRIPT").contains("nohup"))
        XCTAssertTrue(user.hasSuffix(" '' '\(env.path)/logs/swap.log' </dev/null >> '\(env.path)/logs/swap.log' 2>&1 &"), user)
    }

    private func myTempDirs() -> Set<String> {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: "/private/var/tmp")) ?? []
        return Set(names.filter { $0.hasPrefix("luoke-swap.") })
    }

    /// 脚本没起来（macOS 27 上 nohup 那样）：命令等不到「起来了」，退 71、把脚本的输出交出来、删掉临时目录。
    /// 授权框把这个当出错，调用方报「没能起换包脚本」，旧版不退出（以前这时旧版已经退了，什么都没换，临时目录留下）
    func testAdminCommandReportsAScriptThatNeverStarts() throws {
        let l = try layout()
        let before = myTempDirs()
        let cmd = Swap.command(admin: true, waitPID: try deadPID(), current: l.target, staged: l.staged, target: l.target, previous: l.prev,
                               result: l.result, uid: getuid(), requirement: "=identifier \"\(id)\"", log: l.log,
                               script: "echo cannot detach from console; exit 1")
        let started = Date()
        let (rc, out) = try run("/bin/sh", ["-c", cmd])
        XCTAssertEqual(rc, 71, out)
        XCTAssertTrue(out.contains("cannot detach from console"), out)
        XCTAssertLessThan(Date().timeIntervalSince(started), 15)
        XCTAssertEqual(myTempDirs(), before)                       // 临时目录删了
        XCTAssertFalse(FileManager.default.fileExists(atPath: l.result.path))
    }

    /// 清理没起来的临时目录：只删 root 的、超过一天的；别人的（这里是当前用户建的、一样的名字、两天前的）不碰
    func testCleanupLeavesDirsNotOwnedByRoot() throws {
        let mine = "/private/var/tmp/luoke-swap.t" + String(UUID().uuidString.prefix(7))
        try FileManager.default.createDirectory(atPath: mine, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(atPath: mine) }
        try FileManager.default.setAttributes([.modificationDate: Date().addingTimeInterval(-2 * 86400)], ofItemAtPath: mine)
        let l = try layout()
        try makeApp(l.target, marker: "old")
        try makeApp(l.staged, marker: "new")
        let r = try swap(l, current: l.target, admin: true)
        XCTAssertTrue(r.ok, r.why + "\n" + r.log)
        XCTAssertTrue(FileManager.default.fileExists(atPath: mine))
    }

    func testRequirementUsesOurTeamAndBundleID() {
        let r = Swap.requirement(team: "ABCDE12345", identifier: "ai.luoke.core", development: false)
        XCTAssertEqual(r, "=anchor apple generic and identifier \"ai.luoke.core\" and certificate leaf[subject.OU] = \"ABCDE12345\""
                       + " and certificate 1[field.1.2.840.113635.100.6.2.6] and certificate leaf[field.1.2.840.113635.100.6.1.13]")
        XCTAssertEqual(Swap.requirement(team: nil, identifier: "ai.luoke.core.dev", development: true), "=identifier \"ai.luoke.core.dev\"")
        XCTAssertNil(Swap.requirement(team: nil, identifier: "ai.luoke.core", development: false))     // 正式版读不到自己的 Team：不以 root 换包
    }

    // MARK: 真跑

    func testRootPathInstallsTheVerifiedCopy() throws {
        let l = try layout()
        try makeApp(l.target, marker: "old")
        try makeApp(l.staged, marker: "new")
        let r = try swap(l, current: l.target, admin: true)
        XCTAssertTrue(r.ok, r.why + "\n" + r.log)
        XCTAssertEqual(marker(l.target), "new")
        XCTAssertFalse(FileManager.default.fileExists(atPath: l.staged.path))     // 暂存的那份删了
        XCTAssertFalse(FileManager.default.fileExists(atPath: l.prev.path))       // 「应用程序」里的旧版不进用户目录
        let work = try XCTUnwrap(workDir(r.log))
        XCTAssertTrue(work.hasPrefix("/private/var/tmp/luoke-swap."), work)
        XCTAssertFalse(FileManager.default.fileExists(atPath: work))              // 临时目录删了，日志交回了用户的日志
        XCTAssertTrue(r.log.contains("== ") && r.log.contains("done"), r.log)
        let (rc, out) = try run("/usr/bin/codesign", ["--verify", "--deep", "--strict", l.target.path])
        XCTAssertEqual(rc, 0, out)
    }

    /// 攻击：核过签名以后、等旧版退出的时候，同账户的程序把暂存的新版换成自己的程序。root 那一侧核拷过来的那份，不换
    func testRootPathRefusesAStagedCopyChangedDuringTheWait() throws {
        let l = try layout()
        try makeApp(l.target, marker: "old")
        try makeApp(l.staged, marker: "new")
        let exe = l.staged.appendingPathComponent("Contents/MacOS/t")
        try FileManager.default.removeItem(at: exe)
        try "#!/bin/sh\necho replaced\n".write(to: exe, atomically: true, encoding: .utf8)
        let r = try swap(l, current: l.target, admin: true)
        XCTAssertFalse(r.ok)
        XCTAssertTrue(r.why.contains("签名不对"), r.why)
        XCTAssertEqual(marker(l.target), "old")                                   // 装着的那份没动
        XCTAssertFalse(FileManager.default.fileExists(atPath: try XCTUnwrap(workDir(r.log))))
    }

    /// 换成别人签的完整 .app（签名本身没毛病，标识不对）、换成指向别处的链接：都不换
    func testRootPathRefusesOtherAppsAndLinks() throws {
        let l = try layout()
        try makeApp(l.target, marker: "old")
        try makeApp(l.staged, marker: "evil", identifier: "com.example.other")
        var r = try swap(l, current: l.target, admin: true)
        XCTAssertFalse(r.ok)
        XCTAssertTrue(r.why.contains("签名不对"), r.why)
        XCTAssertEqual(marker(l.target), "old")

        try? FileManager.default.removeItem(at: l.staged)
        try? FileManager.default.removeItem(at: l.result)
        let elsewhere = dir.appendingPathComponent("elsewhere.app")
        try makeApp(elsewhere, marker: "linked")
        try FileManager.default.createSymbolicLink(at: l.staged, withDestinationURL: elsewhere)
        r = try swap(l, current: l.target, admin: true)
        XCTAssertFalse(r.ok)
        XCTAssertTrue(r.why.contains("拷不过来"), r.why)
        XCTAssertEqual(marker(l.target), "old")
    }

    /// 已装的那份在用户自己的目录里（~/Applications）、新版放进「应用程序」：旧版以用户身份挪进 previous.noindex，和以前一样
    func testRootPathMovesAUserFolderCopyAsTheUser() throws {
        let l = try layout()
        let mine = dir.appendingPathComponent("HomeApplications/螺壳.app")
        try makeApp(mine, marker: "old")
        try makeApp(l.staged, marker: "new")
        let r = try swap(l, current: mine, admin: true)
        XCTAssertTrue(r.ok, r.why + "\n" + r.log)
        XCTAssertEqual(marker(l.target), "new")
        XCTAssertEqual(marker(l.prev), "old")
        XCTAssertFalse(FileManager.default.fileExists(atPath: mine.path))
    }

    /// 以用户身份换（/Applications 写得进去）：照旧，旧版进 previous.noindex
    func testUserPathStillSwaps() throws {
        let l = try layout()
        try makeApp(l.target, marker: "old")
        try makeApp(l.staged, marker: "new")
        let r = try swap(l, current: l.target, admin: false)
        XCTAssertTrue(r.ok, r.why + "\n" + r.log)
        XCTAssertEqual(marker(l.target), "new")
        XCTAssertEqual(marker(l.prev), "old")
        XCTAssertFalse(FileManager.default.fileExists(atPath: l.staged.path))
    }
}
