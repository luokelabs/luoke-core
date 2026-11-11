import Darwin
import Foundation
import XCTest
@testable import LuokeCore

/// 由本体起的工具（ToolHost.swift）和替扫描弹密码框前的核对（ScanElevate.swift）。不弹框、不碰本机用户的数据：
/// 工具是临时目录里的 shell 脚本，日志写在临时目录。
final class ToolHostTests: XCTestCase {
    var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("toolhost-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    // MARK: 提权的参数

    func testElevateArgs() {
        let me = String(getuid())
        let good = ["--plan", "p-0123456789ab", "--user", me, "--keys", "a0,a12", "--apps", "com.example.app,org.example.tool"]
        XCTAssertTrue(ScanElevate.valid(good))
        XCTAssertTrue(ScanElevate.valid(good + ["--homebrew"]))
        XCTAssertTrue(ScanElevate.valid(["--plan", "p-0123456789ab", "--user", me, "--keys", "a0", "--apps", "", "--homebrew"]))
        let bad: [[String]] = [
            ["--plan", "p-0123456789ab", "--user", me, "--keys", "a0", "--apps", ""],             // 没有应用又没有 --homebrew
            ["--user", me, "--plan", "p-0123456789ab", "--keys", "a0", "--apps", "com.a.b"],       // 顺序不对
            ["--plan", "p-0123456789AB", "--user", me, "--keys", "a0", "--apps", "com.a.b"],       // 大写
            ["--plan", "p-0123456789ab", "--user", String(getuid() + 1), "--keys", "a0", "--apps", "com.a.b"],   // 别人
            ["--plan", "p-0123456789ab", "--user", me, "--keys", "", "--apps", "com.a.b"],
            ["--plan", "p-0123456789ab", "--user", me, "--keys", "a0,b1", "--apps", "com.a.b"],
            ["--plan", "p-0123456789ab", "--user", me, "--keys", "a1234", "--apps", "com.a.b"],
            ["--plan", "p-0123456789ab", "--user", me, "--keys", "a0", "--apps", "com.a"],         // 少一段
            ["--plan", "p-0123456789ab", "--user", me, "--keys", "a0", "--apps", "com.a.b;rm"],
            ["--plan", "p-0123456789ab", "--user", me, "--keys", "a0", "--apps", "com.a.b", "--homebrew", "--homebrew"],
            ["--plan", "p-0123456789ab", "--user", me, "--keys", "a0", "--apps", "com.a.b", "--yes"],
        ]
        for b in bad { XCTAssertFalse(ScanElevate.valid(b), "\(b)") }
    }

    /// 和扫描的 AdminCommand（internal/service/elevate.go）一字不差
    func testAdminCommandMatchesTheScanners() {
        let cmd = ScanElevate.command(helper: "/Users/x/Applications/螺壳扫描.app/Contents/MacOS/luoke-scan-admin",
                                      args: ["--plan", "p-0123456789ab", "--apps", "it's"], team: "TEAM123456")
        let h = "'/Users/x/Applications/螺壳扫描.app/Contents/MacOS/luoke-scan-admin'"
        let copy = "\"$d/luoke-scan-admin\""
        let req = "'=anchor apple generic and identifier \"ai.luoke.scan.admin\" and certificate leaf[subject.OU] = \"TEAM123456\""
            + " and certificate 1[field.1.2.840.113635.100.6.2.6] and certificate leaf[field.1.2.840.113635.100.6.1.13]'"
        XCTAssertEqual(cmd, [
            "umask 077",
            "d=$(/usr/bin/mktemp -d /private/var/tmp/luoke-scan-admin.XXXXXXXX) || exit 70",
            "trap '/bin/rm -rf \"$d\"' EXIT",
            "[ ! -L \(h) ] && [ -f \(h) ] && [ $(/usr/bin/stat -f %z \(h)) -lt 104857600 ] || exit 71",
            "( ulimit -f 204800; /bin/cp -X -P \(h) \(copy) ) || exit 71",
            "[ ! -L \(copy) ] && [ -f \(copy) ] && [ $(/usr/bin/stat -f %z \(copy)) -lt 104857600 ] || exit 71",
            "/usr/bin/codesign --verify --strict -R \(req) \(copy) || exit 72",
            "/usr/bin/env -i PATH=/usr/bin:/bin \(copy) '--plan' 'p-0123456789ab' '--apps' 'it'\\''s' || exit 73",
        ].joined(separator: "; "))
    }

    func testBadArgsNeverReachThePrompt() {
        let r = ScanElevate.answer(["args": ["--plan", "nope"], "prompt": "x"])
        XCTAssertEqual(r["status"] as? String, "failed")
        XCTAssertEqual(r["why"] as? String, "参数不对，没有动任何东西")
        if CodeSigning.selfTeam == nil {       // 临时签名：参数对也不弹框
            let ok = ["--plan", "p-0123456789ab", "--user", String(getuid()), "--keys", "a0", "--apps", "com.a.b"]
            XCTAssertEqual(ScanElevate.answer(["args": ok])["why"] as? String, "开发版不能删系统目录里的东西")
        }
    }

    // MARK: 描述符

    func testPassingADescriptor() throws {
        let (a, b) = try FD.pair()
        let (x, y) = try FD.pair()
        defer { [a, b, y].forEach { close($0) } }
        XCTAssertTrue(FD.send(a, line: ["id": 1, "ok": true], passing: x))
        close(x)
        let (line, fd) = recvLine(b)
        XCTAssertEqual(line.last, 0x0a, "一行，换行结尾")
        XCTAssertEqual(try JSONSerialization.jsonObject(with: line.dropLast()) as? [String: AnyHashable], ["id": 1, "ok": true])
        let got = try XCTUnwrap(fd)
        defer { close(got) }
        XCTAssertTrue(FD.writeAll(got, Data("hi\n".utf8)))
        XCTAssertEqual(FD.Lines(y, limit: 100).next(), Data("hi".utf8))
    }

    /// 只有映射的几个描述符、给的环境、工作目录 /
    func testSpawnLeaksNothing() throws {
        var extra: [Int32] = [-1, -1]
        XCTAssertEqual(pipe(&extra), 0)                      // 没有 CLOEXEC 的：也不能漏过去
        defer { extra.forEach { close($0) } }
        let (out, child) = try FD.pair()
        defer { close(out) }
        let script = "pwd; env | sort; for n in 3 4 5 6 7 8 9 10 11 12; do [ -e /dev/fd/$n ] && echo fd$n; done; true"
        let pid = try Spawn.run("/bin/sh", ["-c", script], env: ["A=1", "PATH=/usr/bin:/bin"], cwd: "/",
                                fds: [(child, 1), (child, 3)])
        close(child)
        var lines: [String] = []
        let r = FD.Lines(out, limit: 1 << 16)
        while let l = r.next() { lines.append(String(decoding: l, as: UTF8.self)) }
        var status: Int32 = 0
        waitpid(pid, &status, 0)
        XCTAssertEqual(lines.first, "/")
        XCTAssertTrue(lines.contains("A=1"))
        XCTAssertFalse(lines.contains { $0.hasPrefix("HOME=") })
        XCTAssertEqual(lines.filter { $0.hasPrefix("fd") }, ["fd3"])
    }

    // MARK: 找工具

    /// 在 `<helpers>/` 放一个假工具（shell 脚本），照螺壳.app 的 Contents/Helpers
    @discardableResult
    func makeTool(_ helpers: URL, name: String = "luoke-fake", script: String = "exit 0") throws -> URL {
        try FileManager.default.createDirectory(at: helpers, withIntermediateDirectories: true)
        let p = helpers.appendingPathComponent(name)
        try Data("#!/bin/sh\n\(script)\n".utf8).write(to: p)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: p.path)
        return p
    }

    let known = ["luoke-fake": HostedTool.Known(identifier: "ai.luoke.fake", program: "luoke-fake", arguments: ["mcp", "--this-mac"],
                                               versionKey: "LuokeFakeVersion")]

    func testFindLooksInsideTheApp() throws {
        let helpers = dir.appendingPathComponent("Helpers", isDirectory: true)
        let p = try makeTool(helpers)
        let t = try XCTUnwrap(HostedTool.find("luoke-fake", helpers: helpers, info: ["LuokeFakeVersion": "0.3.0"], known: known))
        XCTAssertEqual(t.version, "0.3.0")                     // 螺壳打包时记下的版本
        XCTAssertEqual(t.identifier, "ai.luoke.fake")
        XCTAssertEqual(t.arguments, ["mcp", "--this-mac"])
        XCTAssertEqual(t.program.path, p.path)
        XCTAssertNil(HostedTool.find("luoke-fake", helpers: helpers, info: [:], known: known)?.version)   // 开发版没记
        XCTAssertNil(HostedTool.find("luoke-other", helpers: helpers, info: nil, known: known))
        XCTAssertNil(HostedTool.find("luoke-fake", helpers: dir.appendingPathComponent("none"), info: nil, known: known))
        // 程序换成链接：不起
        try FileManager.default.removeItem(at: p)
        try FileManager.default.createSymbolicLink(at: p, withDestinationURL: URL(fileURLWithPath: "/bin/sh"))
        XCTAssertNil(HostedTool.find("luoke-fake", helpers: helpers, info: nil, known: known))
    }

    /// 用户另装的配件（螺壳输入法，luoke-ime#29）：在它自己的 .app 里找，不核版本，不给描述符 3，不借螺壳的授权
    func testFindsAnInstalledAccessorysProgram() throws {
        let app = dir.appendingPathComponent("螺壳输入法.app", isDirectory: true)
        let p = try makeTool(app.appendingPathComponent("Contents/MacOS", isDirectory: true), name: "luoke-ime")
        let known = ["luoke-ime": HostedTool.known["luoke-ime"]!]
        let t = try XCTUnwrap(HostedTool.find("luoke-ime", helpers: dir, info: [:], known: known, installedApp: { $0 == "luoke-ime" ? app : nil }))
        XCTAssertEqual(t.program.path, p.path)
        XCTAssertEqual(t.arguments, ["mcp"])
        XCTAssertEqual(t.identifier, "luoke-ime")
        XCTAssertTrue(t.installed)
        XCTAssertFalse(t.hostChannel)
        XCTAssertEqual(t.access, .disclaimed)
        XCTAssertNil(t.version)
        XCTAssertNil(HostedTool.find("luoke-ime", helpers: dir, info: [:], known: known, installedApp: { _ in nil }))   // 没装
        // 螺壳.app 的 Helpers 里放一个同名的也不算
        _ = try makeTool(dir.appendingPathComponent("Helpers", isDirectory: true), name: "luoke-ime")
        XCTAssertNil(HostedTool.find("luoke-ime", helpers: dir.appendingPathComponent("Helpers"), info: [:], known: known, installedApp: { _ in nil }))
    }

    // MARK: 螺壳记忆：跑在自带的 Python 上

    /// 照螺壳.app 的 Resources/python/bin：真正的程序 python3.12（这里是 shell 脚本），python3 是指它的链接
    func makePython(_ script: String = "exit 0") throws -> URL {
        let bin = dir.appendingPathComponent("python-\(UUID().uuidString)/bin", isDirectory: true)
        try makeTool(bin, name: "python3.12", script: script)
        try FileManager.default.createSymbolicLink(atPath: bin.appendingPathComponent("python3").path, withDestinationPath: "python3.12")
        return bin
    }

    func testMemoryRunsOnTheBundledPython() throws {
        let bin = try makePython()
        let t = try XCTUnwrap(HostedTool.find("luoke-mem", helpers: dir, info: [:], python: bin, memory: { "/Users/x/.luoke/mem.db" }))
        XCTAssertEqual(t.program.path, bin.appendingPathComponent("python3.12").resolvingSymlinksInPath().path)   // 起链接指的那个
        XCTAssertEqual(t.arguments, ["-I", "-B", "-m", "luoke_mem", "--path", "/Users/x/.luoke/mem.db", "mcp"])
        XCTAssertTrue(t.python)
        XCTAssertTrue(t.always)
        XCTAssertFalse(t.hostChannel)
        XCTAssertEqual(t.access, .disclaimed)
        XCTAssertNil(t.version)
        XCTAssertEqual(t.timeout, 120)
        XCTAssertTrue(t.onDemand)
        XCTAssertNil(HostedTool.find("luoke-mem", helpers: dir, info: [:], python: bin, memory: { nil }))          // 本体没带记忆
        XCTAssertNil(HostedTool.find("luoke-mem", helpers: dir, info: [:], python: bin, memory: { "mem.db" }))     // 不是绝对路径
        XCTAssertNil(HostedTool.find("luoke-mem", helpers: dir, info: [:], python: dir.appendingPathComponent("none"), memory: { "/m" }))
        // python3 指到别的目录：不起
        try FileManager.default.removeItem(at: bin.appendingPathComponent("python3"))
        try FileManager.default.createSymbolicLink(atPath: bin.appendingPathComponent("python3").path, withDestinationPath: "/bin/sh")
        XCTAssertNil(HostedTool.find("luoke-mem", helpers: dir, info: [:], python: bin, memory: { "/m" }))
        // 主控跑空闲任务那条路不起它
        XCTAssertEqual(ToolLauncher.run(["luoke-mem", "mcp"]), ToolLauncher.badArguments)
    }

    /// 记忆是本体的一部分：「应用」里没开也给连接；起的就是写死的那串参数，环境照别的工具
    func testMemoryNeedsNoEnablingAndGetsItsArguments() throws {
        let bin = try makePython("""
        read line
        if [ -e /dev/fd/3 ]; then three=yes; else three=no; fi
        printf '{"args":"%s","three":"%s","home":"%s","pwd":"%s"}\\n' "$*" "$three" "$HOME" "$(pwd)"
        cat >/dev/null
        """)
        var t = try XCTUnwrap(HostedTool.find("luoke-mem", helpers: dir, info: [:], python: bin, memory: { "/tmp/mem.db" }))
        t.logFile = dir.appendingPathComponent("mem.log")
        let host = try XCTUnwrap(ToolHost(find: { $0 == "luoke-mem" ? t : nil }, enabled: { _ in false }, check: { _, _ in nil }))
        let agent = dup(host.agentEnd.fileDescriptor)
        defer { close(agent) }
        host.childStarted()
        defer { host.stop() }
        XCTAssertTrue(FD.writeAll(agent, Data("{\"id\":1,\"open\":\"luoke-mem\"}\n".utf8)))
        let (reply, fd) = recvLine(agent)
        XCTAssertEqual(try JSONSerialization.jsonObject(with: reply) as? [String: AnyHashable], ["id": 1, "ok": true])
        let conn = try XCTUnwrap(fd)
        defer { close(conn) }
        XCTAssertTrue(FD.writeAll(conn, Data("{}\n".utf8)))
        shutdown(conn, SHUT_WR)
        let answer = try XCTUnwrap(FD.Lines(conn, limit: 1 << 16).next())
        let got = try XCTUnwrap(try JSONSerialization.jsonObject(with: answer) as? [String: String])
        XCTAssertEqual(got["args"], "-I -B -m luoke_mem --path /tmp/mem.db mcp")
        XCTAssertEqual(got["three"], "no")
        XCTAssertEqual(got["home"], NSHomeDirectory())
        XCTAssertEqual(got["pwd"], "/")
    }

    func testPythonCheckWantsTheWholeAppAndThatPython() throws {
        let bin = try makePython()
        let t = try XCTUnwrap(HostedTool.find("luoke-mem", helpers: dir, info: [:], python: bin, memory: { "/m" }))
        XCTAssertEqual(CodeSigning.checkRunningPython(pid: getpid(), tool: t, team: "T", bundle: { "螺壳的文件被改过" }),
                       "螺壳的文件被改过，不起它")
        XCTAssertNotNil(CodeSigning.checkRunningPython(pid: getpid(), tool: t, team: "T", bundle: { nil }))   // 测试进程不是螺壳团队签的
        XCTAssertTrue(CodeSigning.samePython(bin.appendingPathComponent("python3"), t.program))              // 经链接也算同一个
        XCTAssertFalse(CodeSigning.samePython(URL(fileURLWithPath: "/usr/bin/python3"), t.program))
        XCTAssertFalse(CodeSigning.requirement(identifier: nil, team: "T").contains("identifier"))
        XCTAssertTrue(CodeSigning.requirement(identifier: nil, team: "T").contains("1.2.840.113635.100.6.1.13"))
    }

    func testVersionMustBeTheOneTheAppCarries() {
        XCTAssertTrue(CodeSigning.versionMatches(["CFBundleShortVersionString": "0.3.0"], "0.3.0"))
        XCTAssertFalse(CodeSigning.versionMatches(["CFBundleShortVersionString": "0.2.1"], "0.3.0"))     // 换成了旧版的签名程序
        XCTAssertFalse(CodeSigning.versionMatches(nil, "0.3.0"))
        XCTAssertTrue(CodeSigning.requirement(identifier: "ai.luoke.scan", team: "T").contains("1.2.840.113635.100.6.1.13"))   // 只认 Developer ID
    }

    func testEnabledFollowsTheHubsState() throws {
        let state = dir.appendingPathComponent("state.json")
        XCTAssertFalse(BundledApps.isEnabled("luoke-scan", defaultOn: false, state: state))            // 没有状态文件：默认关
        try JSONSerialization.data(withJSONObject: ["enabled": ["luoke-scan": true]]).write(to: state)
        XCTAssertTrue(BundledApps.isEnabled("luoke-scan", defaultOn: false, state: state))
        XCTAssertFalse(BundledApps.isEnabled("luoke-search", defaultOn: false, state: state))
    }

    // MARK: 搜索：常驻，走同一套核对

    private func script(_ name: String, _ body: String) throws -> URL {
        let u = dir.appendingPathComponent(name)
        try ("#!/bin/sh\n" + body + "\n").write(to: u, atomically: true, encoding: .utf8)
        chmod(u.path, 0o755)
        return u
    }

    private func wait(_ seconds: TimeInterval = 10, until done: () -> Bool) -> Bool {
        let end = Date().addingTimeInterval(seconds)
        while !done() && Date() < end { RunLoop.main.run(until: Date().addingTimeInterval(0.02)) }
        return done()
    }

    func testSearchIsCheckedButNotStartedOnDemand() {
        let k = HostedTool.known["luoke-search"]
        XCTAssertEqual(k?.identifier, "ai.luoke.search")
        XCTAssertEqual(k?.versionKey, "LuokeSearchVersion")
        XCTAssertEqual(k?.onDemand, false)
        XCTAssertEqual(ToolLauncher.run(["luoke-search"]), ToolLauncher.badArguments)
    }

    func testCheckNeedsTheToolInTheApp() {
        XCTAssertNotNil(HostedTool.check("luoke-search", pid: 1, find: { _ in nil }, check: { _, _ in nil }))
    }

    func testCaptureRunsNothingWhenRefused() throws {
        let ran = dir.appendingPathComponent("ran")
        let tool = try script("once", "touch '\(ran.path)'; pwd; echo err >&2; exit 3")
        XCTAssertThrowsError(try Spawn.capture(tool.path, [], env: [], capture: 1) { _ in "不放行" }) { XCTAssertEqual(($0 as? Spawn.Refused)?.why, "不放行") }
        XCTAssertFalse(FileManager.default.fileExists(atPath: ran.path))
        var seen: pid_t = 0
        let out = try Spawn.capture(tool.path, [], env: [], capture: 1) { seen = $0; return nil }
        XCTAssertGreaterThan(seen, 0)
        XCTAssertEqual(String(decoding: out.data, as: UTF8.self), "/\n")
        XCTAssertEqual(out.status >> 8, 3)
        XCTAssertEqual(String(decoding: try Spawn.capture(tool.path, [], env: [], capture: 2) { _ in nil }.data, as: UTF8.self), "err\n")
    }

    func testHelperRefusedDoesNotRunAndIsNotRetried() throws {
        let ran = dir.appendingPathComponent("ran")
        let tool = try script("resident", "echo x >> '\(ran.path)'; exec sleep 30")
        let p = HelperProcess(program: tool, arguments: [], environment: [:], logFile: dir.appendingPathComponent("logs/x.log"))
        p.options.restartDelay = 0.05
        let lock = NSLock()
        var count = 0
        func asked() -> Int { lock.lock(); defer { lock.unlock() }; return count }
        p.check = { _ in lock.lock(); count += 1; lock.unlock(); return "不放行" }      // 后台线程上调
        p.start()
        XCTAssertTrue(wait { asked() == 1 })
        RunLoop.main.run(until: Date().addingTimeInterval(0.4))
        XCTAssertEqual(asked(), 1)
        XCTAssertFalse(p.isRunning)
        XCTAssertFalse(FileManager.default.fileExists(atPath: ran.path))
    }

    func testHelperTalksToTheHostOnFdThree() throws {
        let tool = try script("resident", "echo \"$LUOKE_HOST_FD\" > /dev/null; echo '{\"play\":\"/a\",\"at\":3}' >&3; echo 'not json' >&3; echo '{\"play\":\"/b\"}' >&3; exec sleep 30")
        let p = HelperProcess(program: tool, arguments: [], environment: ["PATH": "/usr/bin:/bin"], logFile: dir.appendingPathComponent("logs/x.log"))
        var got: [String] = []
        p.onHostMessage = { m, _ in got.append(m["play"] as? String ?? "") }
        p.check = { _ in nil }
        p.start()
        XCTAssertTrue(wait { got.count == 2 })
        XCTAssertEqual(got, ["/a", "/b"])
        p.stop()
    }

    func testHelperAnswersHelloOnFdThreeAndDoesNotPassItOn() throws {
        let ran = dir.appendingPathComponent("ran")
        let tool = try script("resident", """
        if [ -S /dev/fd/3 ]; then echo socket > '\(ran.path)'; fi
        printf '{"id": "5e1f0c9a", "method": "hello"}\\n' >&3
        IFS= read -r line <&3
        echo "$line" >> '\(ran.path)'
        echo '{"play":"/a"}' >&3
        exec sleep 30
        """)
        let p = HelperProcess(program: tool, arguments: [], environment: ["PATH": "/usr/bin:/bin"], logFile: dir.appendingPathComponent("logs/x.log"))
        var got: [String] = []
        p.onHostMessage = { m, _ in got.append(m["play"] as? String ?? "?") }
        p.check = { _ in nil }
        p.start()
        XCTAssertTrue(wait { got.count == 1 })
        XCTAssertEqual(got, ["/a"])
        let lines = try String(contentsOf: ran, encoding: .utf8).split(separator: "\n").map(String.init)
        XCTAssertEqual(lines.first, "socket")
        let reply = try JSONSerialization.jsonObject(with: Data(lines[1].utf8)) as? [String: Any]
        XCTAssertEqual(reply?["id"] as? String, "5e1f0c9a")     // 搜索拿随机的 id 认这一句是它刚问的
        XCTAssertEqual((reply?["result"] as? [String: Any])?["status"] as? String, "ok")
        p.stop()
    }

    func testHelperHandsHelloToOnHostRequestWithItsRound() throws {
        let ran = dir.appendingPathComponent("ran")
        let tool = try script("resident", """
        printf '{"id": 0, "method": "hello"}\\n' >&3
        IFS= read -r line <&3
        echo "$line" >> '\(ran.path)'
        exec sleep 30
        """)
        let p = HelperProcess(program: tool, arguments: [], environment: ["PATH": "/usr/bin:/bin"], logFile: dir.appendingPathComponent("logs/x.log"))
        p.onHostMessage = { _, _ in }
        let lock = NSLock()
        var rounds: [Int] = []
        p.onHostRequest = { m, round in
            lock.lock(); rounds.append(round); lock.unlock()
            return ["status": "ok", "token": "t\(round)", "asked": m["method"] as? String ?? "?"]
        }
        p.check = { _ in nil }
        p.start()
        defer { p.stop() }
        XCTAssertTrue(wait { ((try? String(contentsOf: ran, encoding: .utf8)) ?? "").hasSuffix("\n") })
        let reply = try JSONSerialization.jsonObject(with: Data(contentsOf: ran)) as? [String: Any]
        let result = reply?["result"] as? [String: Any]
        XCTAssertEqual(result?["asked"] as? String, "hello")
        lock.lock(); let seen = rounds; lock.unlock()
        XCTAssertEqual(seen.count, 1)
        XCTAssertEqual(result?["token"] as? String, "t\(seen.first ?? -1)")
    }

    func testSearchTokenIsFreshPerRoundAndIgnoresOldRounds() {
        let token = SearchToken()
        XCTAssertNil(token.current)
        let one = token.mint(round: 1)
        XCTAssertEqual(one?.count, 64)
        XCTAssertEqual(one?.allSatisfy { $0.isHexDigit }, true)
        XCTAssertEqual(token.current, one)
        XCTAssertNil(token.mint(round: 0), "已经换过的旧进程拿不到口令")
        XCTAssertEqual(token.current, one)
        let two = token.mint(round: 2)
        XCTAssertNotNil(two)
        XCTAssertNotEqual(two, one)
        XCTAssertEqual(token.current, two)
        token.clear()
        XCTAssertNil(token.current, "搜索不在了就不交出旧口令")
        XCTAssertNil(token.mint(round: 1), "清掉以后旧进程照样拿不到")
        let three = token.mint(round: 2)
        XCTAssertNotNil(three)
        XCTAssertNotEqual(three, two)
        XCTAssertEqual(token.current, three)
    }

    func testCaptureChannelAnswersOnFdThree() throws {
        let tool = try script("once", """
        printf '{"id": 7, "method": "hello"}\\n' >&3
        IFS= read -r line <&3
        echo "$line"
        sleep 30 >/dev/null 2>&1 &
        """)
        let started = Date()
        let out = try Spawn.capture(tool.path, [], env: ["PATH=/usr/bin:/bin"], capture: 1, channel: true) { _ in nil }
        XCTAssertLessThan(Date().timeIntervalSince(started), 10)
        let reply = try JSONSerialization.jsonObject(with: out.data) as? [String: Any]
        XCTAssertEqual(reply?["id"] as? Int, 7)
        XCTAssertEqual((reply?["result"] as? [String: Any])?["status"] as? String, "failed")
        let plain = try Spawn.capture("/bin/sh", ["-c", "if [ -e /dev/fd/3 ]; then echo three; else echo none; fi"], env: [], capture: 1) { _ in nil }
        XCTAssertEqual(String(decoding: plain.data, as: UTF8.self), "none\n")
    }

    func testHelperRestartTakesTheCurrentEnvironment() throws {
        let ran = dir.appendingPathComponent("ran")
        let tool = try script("resident", "echo \"$FROM\" >> '\(ran.path)'; exec sleep 30")
        var from = "one"
        let p = HelperProcess(program: tool, arguments: [], environment: ["FROM": from, "PATH": "/usr/bin:/bin"], logFile: dir.appendingPathComponent("logs/x.log"))
        p.check = { _ in nil }
        p.start()
        XCTAssertTrue(wait { (try? String(contentsOf: ran, encoding: .utf8)) == "one\n" })
        from = "two"
        _ = p.environmentChanged()
        p.restart()
        XCTAssertTrue(wait { (try? String(contentsOf: ran, encoding: .utf8)) == "one\ntwo\n" })
        XCTAssertTrue(wait { p.isRunning })
        p.stop()
    }

    /// 环境改完才起的进程已经是新的：这时轮到的 restart 不用再换，换了等着的活会转给要被杀的进程
    func testHelperRestartLeavesASpawnThatHasTheNewEnvironment() throws {
        let tool = try script("resident", "exec sleep 30")
        let p = HelperProcess(program: tool, arguments: [], environment: ["PATH": "/usr/bin:/bin"], logFile: dir.appendingPathComponent("logs/x.log"))
        let hold = DispatchSemaphore(value: 0), lock = NSLock()
        var checks = 0
        func checked() -> Int { lock.lock(); defer { lock.unlock() }; return checks }
        p.check = { _ in
            lock.lock(); checks += 1; let first = checks == 1; lock.unlock()
            if first { hold.wait() }
            return nil
        }
        _ = p.environmentChanged()
        p.start()
        XCTAssertTrue(wait { checked() == 1 })
        p.restart()
        hold.signal()
        XCTAssertTrue(wait { p.isRunning })
        RunLoop.main.run(until: Date().addingTimeInterval(0.3))
        XCTAssertEqual(checked(), 1)
        p.stop()
    }

    /// 起到一半改了环境：等它起来再换，只有换上的进程才放行等着的活。前一个来不来得及说话都一样
    func testHelperRestartMidSpawnOnlyOpensForTheNewRound() throws {
        let ran = dir.appendingPathComponent("ran")
        let tool = try script("resident", "echo '{\"up\":true}' >&3; echo \"$FROM\" >> '\(ran.path)'; exec sleep 30")
        var from = "one"
        let p = HelperProcess(program: tool, arguments: [], environment: ["FROM": from, "PATH": "/usr/bin:/bin"], logFile: dir.appendingPathComponent("logs/x.log"))
        let hold = DispatchSemaphore(value: 0), lock = NSLock()
        var checks = 0
        func checked() -> Int { lock.lock(); defer { lock.unlock() }; return checks }
        p.check = { _ in
            lock.lock(); checks += 1; let first = checks == 1; lock.unlock()
            if first { hold.wait() }
            return nil
        }
        let gate = RestartGate()
        var said: [Int] = []
        p.onHostMessage = { _, round in said.append(round); gate.up(from: round) }
        p.start()
        XCTAssertTrue(wait { checked() == 1 })

        from = "two"
        let round = p.environmentChanged()
        gate.restarting(round)
        var openedBy: Int?
        gate.whenUp { openedBy = said.last }
        p.restart()
        XCTAssertEqual(checked(), 1)
        hold.signal()

        XCTAssertTrue(wait { openedBy != nil && ((try? String(contentsOf: ran, encoding: .utf8)) ?? "").hasSuffix("two\n") })
        XCTAssertEqual(openedBy, round)
        XCTAssertEqual(checked(), 2)
        XCTAssertTrue(wait { p.isRunning })
        p.stop()
    }

    func testGateRunsAtOnceWhenNothingIsRestarting() {
        let gate = RestartGate(after: { _, _ in })
        var ran = 0
        gate.whenUp { ran += 1 }
        gate.up(from: 3)
        XCTAssertEqual(ran, 1)
    }

    func testGateIgnoresTheProcessBeingReplaced() {
        let gate = RestartGate(after: { _, _ in })
        var ran = 0
        gate.restarting(2)
        gate.whenUp { ran += 1 }
        gate.up(from: 1)
        XCTAssertEqual(ran, 0)
        gate.up(from: 2)
        XCTAssertEqual(ran, 1)
        gate.up(from: 2)
        XCTAssertEqual(ran, 1)
    }

    func testGateGivesUpWaitingAtItsOwnTimeout() {
        var timers: [(TimeInterval, () -> Void)] = []
        let gate = RestartGate(limit: 20, after: { timers.append(($0, $1)) })
        var ran = 0
        gate.restarting(1)
        gate.whenUp { ran += 1 }
        gate.whenUp { ran += 1 }
        XCTAssertEqual(timers.map(\.0), [20])
        timers[0].1()
        XCTAssertEqual(ran, 2)
        gate.whenUp { ran += 1 }
        XCTAssertEqual(ran, 3)
    }

    /// 第一轮的定时器落在第二轮中间：谁也不放。第一轮留下的活跟着第二轮走
    func testGateOldTimerLeavesTheNextRoundAlone() {
        var timers: [() -> Void] = []
        let gate = RestartGate(after: { timers.append($1) })
        var ran: [String] = []
        gate.restarting(1)
        gate.whenUp { ran.append("first") }
        gate.restarting(2)
        gate.whenUp { ran.append("second") }
        timers[0]()
        XCTAssertEqual(ran, [])
        gate.up(from: 1)
        XCTAssertEqual(ran, [])
        timers[1]()
        XCTAssertEqual(ran, ["first", "second"])
    }

    func testGateTimerDoesNothingAfterUp() {
        var timers: [() -> Void] = []
        let gate = RestartGate(after: { timers.append($1) })
        var ran = 0
        gate.restarting(1)
        gate.up(from: 1)
        gate.restarting(2)
        gate.whenUp { ran += 1 }
        timers[0]()
        XCTAssertEqual(ran, 0)
    }

    func testWaitingWorkExpires() {
        let start = Date(timeIntervalSince1970: 1000)
        var ran = 0
        var fresh = ExpiringWork({ ran += 1 }, now: start)
        fresh.take(now: start.addingTimeInterval(599))?()
        XCTAssertEqual(ran, 1)
        XCTAssertNil(fresh.take(now: start.addingTimeInterval(599)), "只拿得到一次")

        var old = ExpiringWork({ ran += 1 }, now: start)
        XCTAssertNil(old.take(now: start.addingTimeInterval(600)))
        XCTAssertNil(old.take(now: start), "过期就丢了")
        XCTAssertEqual(ran, 1)
    }

    func testHelperWithoutHostMessagesHasNoFdThree() throws {
        let ran = dir.appendingPathComponent("ran")
        let tool = try script("resident", "if (echo x >&3) 2>/dev/null; then echo open; else echo closed; fi > '\(ran.path)'; echo \"[$LUOKE_HOST_FD]\" >> '\(ran.path)'; exec sleep 30")
        let p = HelperProcess(program: tool, arguments: [], environment: ["PATH": "/usr/bin:/bin"], logFile: dir.appendingPathComponent("logs/x.log"))
        p.check = { _ in nil }
        p.start()
        XCTAssertTrue(wait { ((try? String(contentsOf: ran, encoding: .utf8)) ?? "").hasSuffix("]\n") })
        XCTAssertEqual(try String(contentsOf: ran, encoding: .utf8), "closed\n[]\n")
        p.stop()
    }

    func testPlayOnlyTakesAnExistingAbsolutePathAndCannotLeaveTheString() {
        let yes: (String) -> Bool = { _ in true }
        XCTAssertNil(SearchHelper.playScript(["play": "relative.mov"], exists: yes))
        XCTAssertNil(SearchHelper.playScript(["play": "/a.mov"], exists: { _ in false }))
        XCTAssertNil(SearchHelper.playScript(["play": 3], exists: yes))
        for path in ["/a.app", "/a.command", "/a.terminal", "/a.sh", "/a", "/a.pdf"] {
            XCTAssertNil(SearchHelper.playScript(["play": path], exists: yes), path)
        }
        for path in ["/a.mov", "/a.MP4", "/a.m4a", "/a.mp3"] {
            XCTAssertNotNil(SearchHelper.playScript(["play": path], exists: yes), path)
        }
        XCTAssertNil(SearchHelper.playScript(["play": "/a\"\ndo shell script \"x"], exists: yes))
        let s = SearchHelper.playScript(["play": "/a\" & \\b.mov", "at": -4], exists: yes)
        XCTAssertEqual(s?.path, "/a\" & \\b.mov")
        XCTAssertTrue(s?.source.contains(#"POSIX file "/a\" & \\b.mov""#) == true)
        XCTAssertTrue(s?.source.contains("set current time of movie to 0.0") == true)
    }

    func testPermissionIsClosedUntilConfirmed() throws {
        let state = dir.appendingPathComponent("state.json")
        XCTAssertEqual(BundledApps.permissionsOk(state: state), [:])
        try Data(#"{"permissions_ok": {"luoke-scan": {"fda": "2026-11-06T07:00:00Z"}}}"#.utf8).write(to: state)
        let ok = BundledApps.permissionsOk(state: state)
        XCTAssertEqual(ok, ["luoke-scan": ["fda": "2026-11-06T07:00:00Z"]])
        XCTAssertEqual(BundledApps.notConfirmed(declared: ["fda"], ok: ok["luoke-scan"] ?? [:]), [], "确认过")
        XCTAssertEqual(BundledApps.notConfirmed(declared: ["photos"], ok: ok["luoke-search"] ?? [:]), ["photos"], "没确认就是没放行")
        try Data("{".utf8).write(to: state)
        XCTAssertEqual(BundledApps.permissionsOk(state: state), [:], "读不出来也算没放行")
    }

    func testPermissionItemIsReadFromTheAppsManifest() throws {
        let scan = dir.appendingPathComponent("scan")
        try FileManager.default.createDirectory(at: scan, withIntermediateDirectories: true)
        try Data(#"{"name": "螺壳扫描", "capabilities": {"permissions": [{"id": "fda", "kind": "system", "owner": "luoke", "title": "完全磁盘访问", "facts": ["读得到所有文件"]}, {"id": "acct", "kind": "account"}]}}"#.utf8)
            .write(to: scan.appendingPathComponent("luoke-accessory.json"))
        XCTAssertEqual(PermissionWindow.item(app: "luoke-scan", id: "fda", resources: dir),
                       PermissionWindow.Item(app: "luoke-scan", appName: "螺壳扫描", id: "fda", title: "完全磁盘访问", facts: ["读得到所有文件"]))
        XCTAssertNil(PermissionWindow.item(app: "luoke-scan", id: "acct", resources: dir))
        XCTAssertNil(PermissionWindow.item(app: "luoke-scan", id: "none", resources: dir))
        XCTAssertNil(PermissionWindow.item(app: "luoke-mail", id: "fda", resources: dir))
        XCTAssertEqual(PermissionWindow.declared(app: "luoke-scan", resources: dir), ["fda"])
        XCTAssertEqual(PermissionWindow.declared(app: "luoke-mail", resources: dir), [], "没声明的不能申请")
    }

    func testConfirmedAndDeniedGoesStraightToSettings() {
        XCTAssertEqual(PermissionWindow.step(state: "denied", confirmed: true), .settings, "确认过：不再看一遍说明")
        XCTAssertEqual(PermissionWindow.step(state: "ask", confirmed: true), .askSystem, "确认过：直接问系统")
        XCTAssertEqual(PermissionWindow.step(state: "granted", confirmed: true), .ready, "两层都有了：什么都不弹")
        XCTAssertEqual(PermissionWindow.step(state: nil, confirmed: true), .window, "查不出：窗口里说清楚")
        for state in ["denied", "ask", "granted", nil] as [String?] {
            XCTAssertEqual(PermissionWindow.step(state: state, confirmed: false), .window, "没确认过：先问用户")
        }
    }

    func testFullDiskAnswersFromTheDatabaseFile() throws {
        let tcc = dir.appendingPathComponent("Library/Application Support/com.apple.TCC")
        try FileManager.default.createDirectory(at: tcc, withIntermediateDirectories: true)
        let db = tcc.appendingPathComponent("TCC.db")
        try Data().write(to: db)
        XCTAssertEqual(HostPermissions.fullDisk(home: dir.path), true)
        try FileManager.default.setAttributes([.posixPermissions: 0], ofItemAtPath: db.path)
        XCTAssertEqual(HostPermissions.fullDisk(home: dir.path), false)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: db.path)
    }

    func testPhotoStatusMaps() {
        XCTAssertEqual(HostPermissions.state(.authorized), "granted")
        XCTAssertEqual(HostPermissions.state(.limited), "granted")
        XCTAssertEqual(HostPermissions.state(.notDetermined), "ask")
        XCTAssertEqual(HostPermissions.state(.denied), "denied")
        XCTAssertEqual(HostPermissions.state(.restricted), "denied")
    }

    func testHelperCheckedRunsAndStops() throws {
        let ran = dir.appendingPathComponent("ran"), log = dir.appendingPathComponent("logs/x.log")
        let tool = try script("resident", "echo \"$FROM $1 $(pwd)\" > '\(ran.path)'; echo said >&2; exec sleep 30")
        let p = HelperProcess(program: tool, arguments: ["arg"], environment: ["FROM": "env", "PATH": "/usr/bin:/bin"], logFile: log)
        var exits = 0
        p.onExit = { exits += 1 }
        p.check = { _ in nil }
        p.start()
        XCTAssertTrue(wait { p.isRunning && FileManager.default.fileExists(atPath: ran.path) })
        XCTAssertEqual(try String(contentsOf: ran, encoding: .utf8), "env arg /\n")
        XCTAssertTrue(wait { (try? String(contentsOf: log, encoding: .utf8)) == "said\n" })
        p.stop()
        XCTAssertFalse(p.isRunning)
        XCTAssertEqual(exits, 1)
    }

    func testHelperCheckedRestartsAfterACrash() throws {
        let ran = dir.appendingPathComponent("ran")
        let tool = try script("crashes", "echo x >> '\(ran.path)'; kill -9 $$")
        let p = HelperProcess(program: tool, arguments: [], environment: ["PATH": "/usr/bin:/bin"], logFile: dir.appendingPathComponent("logs/x.log"))
        p.options.restartDelay = 0.05
        p.options.maxRestarts = 1
        p.check = { _ in nil }
        p.start()
        XCTAssertTrue(wait { ((try? String(contentsOf: ran, encoding: .utf8)) ?? "").split(separator: "\n").count == 2 })
        RunLoop.main.run(until: Date().addingTimeInterval(0.3))
        XCTAssertEqual(((try? String(contentsOf: ran, encoding: .utf8)) ?? "").split(separator: "\n").count, 2)
        p.stop()
    }

    /// 测试进程有 bundle id，又不是开发版：和正式版一样，这几个环境变量都不认
    func testReleaseBuildIgnoresTheOverrides() {
        XCTAssertFalse(AppInfo.allowsCodeOverrides)
        for key in ["LUOKE_RECIPE", "LUOKE_RELEASE", "LUOKE_PYTHON", "LUOKE_CORE_PATH"] { setenv(key, "/tmp/somewhere-else", 1) }
        defer { for key in ["LUOKE_RECIPE", "LUOKE_RELEASE", "LUOKE_PYTHON", "LUOKE_CORE_PATH"] { unsetenv(key) } }
        XCTAssertTrue(AppInfo.recipe.hasSuffix("recipe.json"))
        XCTAssertTrue(AppInfo.release.hasSuffix("release.json"))
        XCTAssertTrue(AppInfo.python.hasSuffix("python/bin/python3"))
        XCTAssertNil(AppInfo.pythonPath)
    }

    /// 实验室、换更新源、任意位置的副本、不当安装器：正式版设了环境变量也不认，只认 bundle id
    func testReleaseBuildIgnoresLabAndUpdateOverrides() {
        let keys = ["LUOKE_UPDATE_API", "LUOKE_UPDATE_MIRROR_API", "LUOKE_ALLOW_ANY_COPY", "LUOKE_NO_INSTALLER"]
        for key in keys { setenv(key, "1", 1) }
        defer { for key in keys { unsetenv(key) } }
        for key in keys { XCTAssertNil(AppInfo.devEnvironment(key), key) }
        XCTAssertNil(Updater.testAPI)
        XCTAssertNil(Updater.testMirrorAPI)
        XCTAssertTrue(Updater.api(page: 1).hasPrefix("https://api.github.com/repos/luokelabs/"))

        XCTAssertFalse(AppInfo.labBuild(bundleID: "ai.luoke.core", environment: ["LUOKE_LAB": "1"]))
        XCTAssertFalse(AppInfo.labBuild(bundleID: "ai.luoke.core", environment: [:]))
        XCTAssertTrue(AppInfo.labBuild(bundleID: "ai.luoke.core.lab", environment: [:]))
        XCTAssertTrue(AppInfo.labBuild(bundleID: "ai.luoke.core.dev", environment: ["LUOKE_LAB": "1"]))
        XCTAssertFalse(AppInfo.labBuild(bundleID: "ai.luoke.core.dev", environment: [:]))
        XCTAssertTrue(AppInfo.labBuild(bundleID: "", environment: ["LUOKE_LAB": "1"]))      // swift run，没打包
    }

    func testDefaultComesFromTheManifest() throws {
        func manifest(_ name: String, _ o: [String: Any]) throws {
            let d = dir.appendingPathComponent(name, isDirectory: true)
            try FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
            try JSONSerialization.data(withJSONObject: o).write(to: d.appendingPathComponent("luoke-accessory.json"))
        }
        try manifest("mail", ["default_on": true])
        try manifest("scan", ["name": "螺壳扫描"])
        XCTAssertTrue(BundledApps.defaultOn("luoke-mail", resources: dir))
        XCTAssertFalse(BundledApps.defaultOn("luoke-scan", resources: dir))         // 没写：关
        XCTAssertFalse(BundledApps.defaultOn("luoke-search", resources: dir))       // 没有清单：关
    }

    func testAppLogsLoseMailTextAddressesAndTheHomeFolder() {
        XCTAssertEqual(Diagnostics.redacted("2026-11-05 [luoke-mail] notification (body, posted): 螺壳邮件助手：来自某人的账单"),
                       "2026-11-05 [luoke-mail] notification (body, posted)")
        XCTAssertEqual(Diagnostics.redacted("[luoke-mail] one-click unsubscribe refused for 某商店: 403"), "[luoke-mail] one-click unsubscribe refused for")
        XCTAssertEqual(Diagnostics.redacted("[luoke-mail] could not restore label '私人': x"), "[luoke-mail] could not restore label")
        XCTAssertEqual(Diagnostics.redacted("IMAP login refused: a.b+c@example.com"), "IMAP login refused: <邮箱>")
        XCTAssertEqual(Diagnostics.redacted("打不开 /" + "Users/someone/Applications/x.app"), "打不开 ~/Applications/x.app")
        XCTAssertEqual(Diagnostics.redacted("luoke-agent: 这一轮出错 TimeoutError"), "luoke-agent: 这一轮出错 TimeoutError")
    }

    func testAppLogTailKeepsTheEndAndSurvivesBadBytes() throws {
        let log = dir.appendingPathComponent("x.log")
        XCTAssertEqual(Diagnostics.appLogTail(log), "（无）")
        var data = Data("一\n二\n".utf8)
        data.append(contentsOf: [0xFF, 0x0A])
        data.append(Data("三\n".utf8))
        try data.write(to: log)
        XCTAssertTrue(Diagnostics.appLogTail(log, lines: 2).hasSuffix("\n三"))
        XCTAssertFalse(Diagnostics.appLogTail(log, lines: 2).contains("二"))
    }

    func testNoteGoesIntoTheHubLogWithoutTheHomeFolder() throws {
        let log = dir.appendingPathComponent("logs/hub.log")
        BundledApps.note("螺壳搜索：旧的 /" + "Users/someone/Applications/螺壳搜索.app 挪进了废纸篓，结束了 1 个进程", log: log)
        BundledApps.note("第二行", log: log)
        let lines = try String(contentsOf: log, encoding: .utf8).split(separator: "\n")
        XCTAssertEqual(lines.count, 2)
        XCTAssertTrue(lines[0].hasSuffix("螺壳搜索：旧的 ~/Applications/螺壳搜索.app 挪进了废纸篓，结束了 1 个进程"))
    }

    func testAdminHelperIsTheAppsOwn() throws {
        let helpers = dir.appendingPathComponent("Helpers", isDirectory: true)
        XCTAssertNil(ScanElevate.adminHelper(helpers: helpers))
        try FileManager.default.createDirectory(at: helpers, withIntermediateDirectories: true)
        let admin = helpers.appendingPathComponent("luoke-scan-admin")
        try FileManager.default.createSymbolicLink(at: admin, withDestinationURL: URL(fileURLWithPath: "/bin/sh"))
        XCTAssertNil(ScanElevate.adminHelper(helpers: helpers))                                       // 链接不认
        try FileManager.default.removeItem(at: admin)
        try Data("x".utf8).write(to: admin)
        XCTAssertEqual(ScanElevate.adminHelper(helpers: helpers), admin.path)
    }

    func testElevateChildAcceptsOnlyPath() {
        let p = ScanElevate.pathValue
        XCTAssertTrue(ScanElevate.environmentOK(["PATH": p]))
        XCTAssertTrue(ScanElevate.environmentOK(["PATH": p, "__CF_USER_TEXT_ENCODING": "0x1F5:0x19:0x34"]))   // CoreFoundation 自己加的
        XCTAssertFalse(ScanElevate.environmentOK(["PATH": p, "PS4": "$(id)"]))
        XCTAssertFalse(ScanElevate.environmentOK(["PATH": "/tmp:" + p]))
        XCTAssertFalse(ScanElevate.environmentOK([:]))
    }

    // MARK: 工具请本体叫到前面（HostRequests.swift）：假的 Env，不开访达、不开系统设置

    final class FakeFront {
        var front = true
        var revealed: [URL] = []
        var opened: [URL] = []
        var opens = true
        lazy var env = HostRequests.Env(recent: { [unowned self] in self.front }, exists: { FileManager.default.fileExists(atPath: $0) },
                                        reveal: { [unowned self] in self.revealed.append($0) },
                                        open: { [unowned self] in self.opened.append($0); return self.opens }, wait: 5)
    }

    func testRevealOnlyTakesAnExistingAbsolutePath() throws {
        let file = dir.appendingPathComponent("a b.txt")
        try Data().write(to: file)
        let yes: (String) -> Bool = { _ in true }
        XCTAssertEqual(HostRequests.revealPath(["path": file.path], exists: { FileManager.default.fileExists(atPath: $0) }), file.path)
        XCTAssertNil(HostRequests.revealPath(["path": dir.appendingPathComponent("none").path], exists: { FileManager.default.fileExists(atPath: $0) }))
        XCTAssertNil(HostRequests.revealPath(["path": "relative/x"], exists: yes))
        XCTAssertNil(HostRequests.revealPath(["path": "/a\nb"], exists: yes))
        XCTAssertNil(HostRequests.revealPath(["path": "/a\0b"], exists: yes))
        XCTAssertNil(HostRequests.revealPath(["path": "/" + String(repeating: "x", count: Int(PATH_MAX))], exists: yes))
        XCTAssertNil(HostRequests.revealPath(["path": 3], exists: yes))
        XCTAssertNil(HostRequests.revealPath([:], exists: yes))
    }

    func testRevealOnlyRightAfterAClickInLuoke() throws {
        let fake = FakeFront()
        fake.front = false
        XCTAssertEqual(HostRequests.reveal(["path": dir.path], env: fake.env)["status"] as? String, "failed")
        XCTAssertEqual(fake.revealed, [])
        fake.front = true
        XCTAssertEqual(HostRequests.reveal(["path": dir.path], env: fake.env)["status"] as? String, "done")
        XCTAssertEqual(fake.revealed, [URL(fileURLWithPath: dir.path)])
        XCTAssertEqual(HostRequests.reveal(["path": dir.appendingPathComponent("gone").path], env: fake.env)["status"] as? String, "failed")
        XCTAssertEqual(fake.revealed.count, 1)
    }

    func testSettingsOnlyKnownPanesAndTheHostBuildsTheAddress() {
        let fake = FakeFront()
        XCTAssertEqual(HostRequests.openSettings(["pane": "Privacy_AllFiles"], env: fake.env)["status"] as? String, "done")
        XCTAssertEqual(HostRequests.openSettings(["pane": "LoginItems"], env: fake.env)["status"] as? String, "done")
        XCTAssertEqual(HostRequests.openSettings(["pane": "Privacy_Contacts"], env: fake.env)["status"] as? String, "done")
        XCTAssertEqual(HostRequests.openSettings(["pane": "Privacy"], env: fake.env)["status"] as? String, "done")
        XCTAssertEqual(fake.opened.map(\.absoluteString), ["x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
                                                           "x-apple.systempreferences:com.apple.LoginItems-Settings.extension",
                                                           "x-apple.systempreferences:com.apple.preference.security?Privacy_Contacts",
                                                           "x-apple.systempreferences:com.apple.preference.security?Privacy"])
        for bad in ["Privacy_Nope", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles", "https://example.com", 1] as [Any] {
            XCTAssertEqual(HostRequests.openSettings(["pane": bad], env: fake.env)["status"] as? String, "failed", "\(bad)")
        }
        XCTAssertEqual(HostRequests.openSettings(["url": "https://example.com"], env: fake.env)["status"] as? String, "failed")
        XCTAssertEqual(fake.opened.count, 4)
        fake.front = false
        XCTAssertEqual(HostRequests.openSettings(["pane": "FDE"], env: fake.env)["status"] as? String, "failed")
        fake.front = true
        fake.opens = false
        XCTAssertEqual(HostRequests.openSettings(["pane": "FDE"], env: fake.env)["status"] as? String, "failed")
    }

    /// 描述符 3 上一问一答：reveal、open_settings 哪个工具都能请，elevate 只给扫描，不认识的答 failed
    func testHostChannelAnswersEachRequestById() throws {
        let (host, tool) = try FD.pair()
        defer { close(tool) }
        let fake = FakeFront()
        let done = DispatchSemaphore(value: 0)
        let other = HostedTool(accessory: "luoke-fake", identifier: "ai.luoke.fake", version: nil, dir: dir, program: dir, arguments: [])
        ScanElevate.listen(host, tool: other, done: done, requests: fake.env)
        let lines = FD.Lines(tool, limit: 1 << 16)
        func ask(_ id: Int, _ method: String, _ params: [String: Any]) throws -> [String: Any] {
            XCTAssertTrue(FD.writeLine(tool, ["id": id, "method": method, "params": params]))
            let o = try XCTUnwrap(try JSONSerialization.jsonObject(with: XCTUnwrap(lines.next())) as? [String: Any])
            XCTAssertEqual(o["id"] as? Int, id)
            return try XCTUnwrap(o["result"] as? [String: Any])
        }
        // 主线程在这里等答复：监听那边要上主线程做，等的时候让主线程跑起来
        func answer(_ id: Int, _ method: String, _ params: [String: Any]) throws -> [String: Any] {
            var got: [String: Any]?
            var err: Error?
            let finished = DispatchSemaphore(value: 0)
            DispatchQueue.global().async {
                do { got = try ask(id, method, params) } catch { err = error }
                finished.signal()
            }
            while finished.wait(timeout: .now()) == .timedOut { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
            if let err { throw err }
            return try XCTUnwrap(got)
        }
        XCTAssertEqual(try answer(1, "reveal", ["path": dir.path])["status"] as? String, "done")
        XCTAssertEqual(try answer(2, "open_settings", ["pane": "Privacy_Accessibility"])["status"] as? String, "done")
        XCTAssertEqual(try answer(3, "elevate", ["args": [], "prompt": ""])["why"] as? String, "这个工具不能请螺壳弹密码框")
        XCTAssertEqual(try answer(4, "open", ["url": "https://example.com"])["why"] as? String, "不认识的请求")
        XCTAssertEqual(fake.revealed, [URL(fileURLWithPath: dir.path)])
        XCTAssertEqual(fake.opened.count, 1)
        shutdown(tool, SHUT_WR)
        XCTAssertEqual(done.wait(timeout: .now() + 5), .success)
        close(host)
    }

    func testReleaseRefusesAToolThatRanBeforeTheCheck() throws {
        let helpers = dir.appendingPathComponent("Helpers", isDirectory: true)
        let ran = dir.appendingPathComponent("ran")
        let p = try makeTool(helpers, script: "touch '\(ran.path)'\nwhile :; do :; done")
        let devnull = open("/dev/null", O_RDWR | O_CLOEXEC)
        defer { close(devnull) }
        let pid = try Spawn.run(p.path, [], env: ["PATH=/usr/bin:/bin"], cwd: "/", suspended: true, fds: [(devnull, 0), (devnull, 1), (devnull, 2)])
        defer { kill(pid, SIGKILL); var st: Int32 = 0; _ = waitpid(pid, &st, WNOHANG) }
        XCTAssertTrue(Spawn.untouched(pid), "刚起、停着：一条指令都没跑")
        kill(pid, SIGCONT)                       // 别的进程抢在核完以前放它出来，跑了一阵再停住
        for _ in 0..<200 where !FileManager.default.fileExists(atPath: ran.path) { usleep(50_000) }   // 机器忙时 nice 过的进程可能半天轮不上
        XCTAssertTrue(FileManager.default.fileExists(atPath: ran.path))
        kill(pid, SIGSTOP)
        usleep(100_000)
        XCTAssertFalse(Spawn.untouched(pid))
        XCTAssertNotNil(Spawn.release(pid, check: { nil }))
        var status: Int32 = 0
        XCTAssertEqual(waitpid(pid, &status, WNOHANG), -1, "已经结束、收掉了")
    }

    func testLogIsCapped() throws {
        let log = dir.appendingPathComponent("x.log")
        try Data(count: Int(FD.logLimit) + 10).write(to: log)
        let fd = FD.appendLog(log)
        XCTAssertGreaterThanOrEqual(fd, 0)
        close(fd)
        XCTAssertEqual((try FileManager.default.attributesOfItem(atPath: log.path)[.size] as? NSNumber)?.intValue, 0)
        XCTAssertTrue(FileManager.default.fileExists(atPath: log.path + ".1"))
    }

    // MARK: 整条路：要连接、核过再放行、一条一答

    func tool(script: String) throws -> HostedTool {
        let helpers = dir.appendingPathComponent("helpers-\(UUID().uuidString)", isDirectory: true)
        try makeTool(helpers, script: script)
        var t = try XCTUnwrap(HostedTool.find("luoke-fake", helpers: helpers, info: nil, known: known))
        t.logFile = dir.appendingPathComponent("tool.log")
        return t
    }

    func testAskForAConnectionThenOneRequest() throws {
        let marker = dir.appendingPathComponent("ran")
        // 假工具：一起来就留个记号；读一行，答一行（带上它看到的 LUOKE_HOST_FD、有没有描述符 3），读到结尾退出
        let script = """
        touch '\(marker.path)'
        read line
        if [ -e /dev/fd/3 ]; then three=yes; else three=no; fi
        printf '{"jsonrpc":"2.0","id":1,"result":{"fd":"%s","three":"%s","args":"%s"}}\\n' "$LUOKE_HOST_FD" "$three" "$*"
        cat >/dev/null
        """
        let t = try tool(script: script)
        var stoppedWhileChecked = false
        let host = try XCTUnwrap(ToolHost(find: { $0 == "luoke-fake" ? t : nil }, enabled: { _ in true }, check: { _, _ in
            usleep(300_000)                                      // 停着的时候：它一行都还没跑
            stoppedWhileChecked = !FileManager.default.fileExists(atPath: marker.path)
            return nil
        }))
        let agent = dup(host.agentEnd.fileDescriptor)
        defer { close(agent) }
        host.childStarted()
        defer { host.stop() }

        XCTAssertTrue(FD.writeAll(agent, Data("{\"id\":7,\"open\":\"luoke-fake\"}\n".utf8)))
        let (reply, fd) = recvLine(agent)
        XCTAssertEqual(try JSONSerialization.jsonObject(with: reply) as? [String: AnyHashable], ["id": 7, "ok": true])
        XCTAssertTrue(stoppedWhileChecked, "核的时候它是停着的")
        let conn = try XCTUnwrap(fd)
        defer { close(conn) }
        XCTAssertTrue(FD.writeAll(conn, Data("{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}\n".utf8)))
        shutdown(conn, SHUT_WR)
        let answer = try XCTUnwrap(FD.Lines(conn, limit: 1 << 16).next())
        let result = try XCTUnwrap((try JSONSerialization.jsonObject(with: answer) as? [String: Any])?["result"] as? [String: String])
        XCTAssertEqual(result, ["fd": "3", "three": "yes", "args": "mcp --this-mac"])

        XCTAssertTrue(FD.writeAll(agent, Data("{\"id\":8,\"open\":\"luoke-search\"}\n".utf8)))
        let (refused, none) = recvLine(agent)
        XCTAssertNil(none, "不给的时候不带描述符")
        XCTAssertNotNil((try JSONSerialization.jsonObject(with: refused) as? [String: Any])?["error"] as? String)
    }

    func testBadSignatureKillsItBeforeItRuns() throws {
        let marker = dir.appendingPathComponent("ran")
        let t = try tool(script: "touch '\(marker.path)'")
        let host = try XCTUnwrap(ToolHost(find: { _ in t }, enabled: { _ in true }, check: { _, _ in "它的签名不对，不起它" }))
        let agent = dup(host.agentEnd.fileDescriptor)
        defer { close(agent) }
        host.childStarted()
        defer { host.stop() }
        XCTAssertTrue(FD.writeAll(agent, Data("{\"id\":1,\"open\":\"luoke-fake\"}\n".utf8)))
        let (reply, fd) = recvLine(agent)
        XCTAssertNil(fd)
        XCTAssertEqual((try JSONSerialization.jsonObject(with: reply) as? [String: Any])?["error"] as? String, "它的签名不对，不起它")
        usleep(200_000)
        XCTAssertFalse(FileManager.default.fileExists(atPath: marker.path), "停着的时候就被结束了，一行没跑")
    }

    func testNotEnabledNeverStarts() throws {
        let marker = dir.appendingPathComponent("ran")
        let t = try tool(script: "touch '\(marker.path)'")
        let host = try XCTUnwrap(ToolHost(find: { _ in t }, enabled: { _ in false }, check: { _, _ in nil }))
        let agent = dup(host.agentEnd.fileDescriptor)
        defer { close(agent) }
        host.childStarted()
        defer { host.stop() }
        XCTAssertTrue(FD.writeAll(agent, Data("{\"id\":1,\"open\":\"luoke-fake\"}\n".utf8)))
        let (reply, fd) = recvLine(agent)
        XCTAssertNil(fd)
        XCTAssertNotNil((try JSONSerialization.jsonObject(with: reply) as? [String: Any])?["error"] as? String)
        usleep(200_000)
        XCTAssertFalse(FileManager.default.fileExists(atPath: marker.path))
    }

    func testHostAnswersTokenRequestsWithoutAConnection() throws {
        let host = try XCTUnwrap(ToolHost(find: { _ in nil }, enabled: { _ in true }, check: { _, _ in nil },
                                          token: { $0 == "luoke-search" ? "abc" : nil }))
        let agent = dup(host.agentEnd.fileDescriptor)
        defer { close(agent) }
        host.childStarted()
        defer { host.stop() }
        XCTAssertTrue(FD.writeAll(agent, Data("{\"id\":3,\"token\":\"luoke-search\"}\n".utf8)))
        let (reply, fd) = recvLine(agent)
        XCTAssertNil(fd)
        XCTAssertEqual(try JSONSerialization.jsonObject(with: reply) as? [String: AnyHashable], ["id": 3, "token": "abc"])
        XCTAssertTrue(FD.writeAll(agent, Data("{\"id\":4,\"token\":\"luoke-scan\"}\n".utf8)))
        let (other, none) = recvLine(agent)
        XCTAssertNil(none)
        let refused = try JSONSerialization.jsonObject(with: other) as? [String: Any]
        XCTAssertEqual(refused?["id"] as? Int, 4)
        XCTAssertNil(refused?["token"])
        XCTAssertNil(refused?["ok"])
        XCTAssertNotNil(refused?["error"] as? String)
    }

    // MARK: 主控跑空闲任务（螺壳 --run-tool）

    func testRunToolTakesOnlyTheWrittenJobs() {
        let scan = HostedTool.known["luoke-scan"]!
        XCTAssertTrue(ToolLauncher.valid(["update", "--this-mac"], known: scan))
        XCTAssertTrue(ToolLauncher.valid(["scan", "--this-mac", "--autoclean"], known: scan))
        XCTAssertTrue(ToolLauncher.valid(["scan", "--this-mac", "--quick"], known: scan))     // 两轮之间看一眼新下载的
        XCTAssertFalse(ToolLauncher.valid([], known: scan))
        XCTAssertFalse(ToolLauncher.valid(["update"], known: scan))                          // 一字不差
        XCTAssertFalse(ToolLauncher.valid(["update", "--this-mac", "--autoclean"], known: scan))
        XCTAssertFalse(ToolLauncher.valid(["scan", "--this-mac"], known: scan))
        XCTAssertFalse(ToolLauncher.valid(["scan", "--quick"], known: scan))
        XCTAssertFalse(ToolLauncher.valid(["scan", "--quick", "--this-mac"], known: scan))    // 顺序也要一样
        XCTAssertFalse(ToolLauncher.valid(["scan", "--this-mac", "--quick", "--autoclean"], known: scan))
        XCTAssertFalse(ToolLauncher.valid(["scan", "--this-mac", "--quick", "--json"], known: scan))
        XCTAssertFalse(ToolLauncher.valid(["mcp", "--this-mac"], known: scan))               // 不是空闲任务
        XCTAssertFalse(ToolLauncher.valid(["update", "/etc"], known: scan))
    }

    func testRunToolPassesTheToolsExitCode() {
        XCTAssertEqual(ToolLauncher.exitCode(0), 0)
        XCTAssertEqual(ToolLauncher.exitCode(3 << 8), 3)
        XCTAssertEqual(ToolLauncher.exitCode(SIGTERM), 128 + SIGTERM)
        XCTAssertEqual(ToolLauncher.exitCode(SIGKILL), 128 + SIGKILL)
    }

    /// 假工具：shell 脚本
    func fakeTool(_ body: String) throws -> HostedTool {
        let helpers = dir.appendingPathComponent("Helpers-\(UUID().uuidString)", isDirectory: true)
        let program = try makeTool(helpers, script: body)
        return HostedTool(accessory: "luoke-fake", identifier: "ai.luoke.fake", version: nil, dir: helpers, program: program, arguments: [])
    }

    var fakeKnown: [String: HostedTool.Known] {
        ["luoke-fake": HostedTool.Known(identifier: "ai.luoke.fake", program: "luoke-fake", arguments: [], jobs: [["update", "--this-mac"], ["update"]])]
    }

    func testRunToolRunsTheJobWithAStrippedEnvironment() throws {
        let out = dir.appendingPathComponent("out.txt")
        let tool = try fakeTool("echo \"$@\" > '\(out.path)'; env | sort >> '\(out.path)'; pwd >> '\(out.path)'; exit 3")
        setenv("LUOKE_SECRET_TEST", "x", 1)
        defer { unsetenv("LUOKE_SECRET_TEST") }
        var checked: pid_t = 0
        let code = ToolLauncher.run(["luoke-fake", "update", "--this-mac"], known: fakeKnown, find: { _ in tool }, enabled: { _ in true },
                                    check: { pid, _ in checked = pid; return nil })
        XCTAssertEqual(code, 3)
        XCTAssertGreaterThan(checked, 0)
        let lines = try String(contentsOf: out, encoding: .utf8).split(separator: "\n").map(String.init)
        XCTAssertEqual(lines.first, "update --this-mac")
        XCTAssertEqual(lines.last, "/")
        let names = Set(lines.dropFirst().dropLast().compactMap { $0.split(separator: "=").first.map(String.init) })
        XCTAssertTrue(names.isSubset(of: ["HOME", "TMPDIR", "PATH", "LANG", "LUOKE_HUB_SOCKET", "PWD", "SHLVL", "_"]), "\(names)")
        XCTAssertFalse(names.contains("LUOKE_HOST_FD"))          // 空闲任务不提权
    }

    func testRunToolGivesASocketOnFd3ThatRefusesEverything() throws {
        let out = dir.appendingPathComponent("fd3.txt")
        let tool = try fakeTool("""
        if [ -S /dev/fd/3 ]; then echo socket > '\(out.path)'; else echo none > '\(out.path)'; fi
        printf '{"id": 0, "method": "hello"}\\n' >&3
        IFS= read -r line <&3
        echo "$line" >> '\(out.path)'
        sleep 30 &
        exit 0
        """)
        let started = Date()
        let code = ToolLauncher.run(["luoke-fake", "update"], known: fakeKnown, find: { _ in tool }, enabled: { _ in true }, check: { _, _ in nil })
        XCTAssertEqual(code, 0)
        XCTAssertLessThan(Date().timeIntervalSince(started), 10)      // 工具起的进程还拿着描述符 3，也不等它
        let lines = try String(contentsOf: out, encoding: .utf8).split(separator: "\n").map(String.init)
        XCTAssertEqual(lines.first, "socket")
        let reply = try JSONSerialization.jsonObject(with: Data(lines[1].utf8)) as? [String: Any]
        XCTAssertEqual(reply?["id"] as? Int, 0)
        XCTAssertEqual((reply?["result"] as? [String: Any])?["status"] as? String, "failed")
    }

    func testRunToolRefusesBeforeTheToolRuns() throws {
        let marker = dir.appendingPathComponent("ran")
        let tool = try fakeTool("touch '\(marker.path)'")
        func run(_ args: [String], find: HostedTool? = nil, enabled: Bool = true, check: String? = nil) -> Int32 {
            ToolLauncher.run(args, known: fakeKnown, find: { _ in find }, enabled: { _ in enabled }, check: { _, _ in check })
        }
        XCTAssertEqual(run(["luoke-fake", "update"], find: tool, check: "签名不对"), ToolLauncher.checkFailed)
        XCTAssertEqual(run(["luoke-fake", "mcp"], find: tool), ToolLauncher.badArguments)
        XCTAssertEqual(run(["luoke-other", "update"], find: tool), ToolLauncher.badArguments)
        XCTAssertEqual(run(["luoke-fake", "update"], find: nil), ToolLauncher.notInstalled)
        XCTAssertEqual(run(["luoke-fake", "update"], find: tool, enabled: false), ToolLauncher.notInstalled)
        XCTAssertFalse(FileManager.default.fileExists(atPath: marker.path))
    }

    func testRunToolForwardsSigtermToTheTool() throws {
        let ready = dir.appendingPathComponent("ready")
        let tool = try fakeTool("trap 'exit 7' TERM\ntouch '\(ready.path)'\ni=0\nwhile [ $i -lt 100 ]; do sleep 0.1; i=$((i+1)); done\nexit 9")
        var code: Int32 = -1
        let done = expectation(description: "工具退出")
        Thread {
            code = ToolLauncher.run(["luoke-fake", "update"], known: self.fakeKnown, find: { _ in tool }, enabled: { _ in true }, check: { _, _ in nil })
            done.fulfill()
        }.start()
        for _ in 0..<200 where !FileManager.default.fileExists(atPath: ready.path) { usleep(50_000) }
        guard FileManager.default.fileExists(atPath: ready.path) else { return XCTFail("工具没起来") }   // 没在跑时不发：会结束测试进程
        kill(getpid(), SIGTERM)                 // 主控让它收尾：发给 --run-tool 这个进程，转给工具
        wait(for: [done], timeout: 15)
        XCTAssertEqual(code, 7)
    }

    // MARK: 帮手

    /// recvmsg 收一行，连同随它来的描述符
    func recvLine(_ sock: Int32) -> (Data, Int32?) {
        var buf = [UInt8](repeating: 0, count: 4096)
        var control = [UInt8](repeating: 0, count: 64)
        var got: Int32?
        let n: Int = buf.withUnsafeMutableBytes { b in
            control.withUnsafeMutableBytes { c in
                var iov = iovec(iov_base: b.baseAddress, iov_len: b.count)
                return withUnsafeMutablePointer(to: &iov) { iovp in
                    var msg = msghdr(msg_name: nil, msg_namelen: 0, msg_iov: iovp, msg_iovlen: 1,
                                     msg_control: c.baseAddress, msg_controllen: socklen_t(c.count), msg_flags: 0)
                    let n = recvmsg(sock, &msg, 0)
                    if msg.msg_controllen >= socklen_t(MemoryLayout<cmsghdr>.size + 4) {
                        let cmsg = c.baseAddress!.assumingMemoryBound(to: cmsghdr.self)
                        if cmsg.pointee.cmsg_level == SOL_SOCKET && cmsg.pointee.cmsg_type == SCM_RIGHTS {
                            got = (c.baseAddress! + MemoryLayout<cmsghdr>.size).load(as: Int32.self)
                        }
                    }
                    return n
                }
            }
        }
        return (Data(buf[0..<max(n, 0)]), got)
    }

    func testFirstAnswerTakesTheAnswerThatCameInTime() {
        XCTAssertEqual(FirstRunModel.firstAnswer(within: 1) { (answer: @escaping (String) -> Void) in answer("ok") }, "ok")
        let later = FirstRunModel.firstAnswer(within: 1) { (answer: @escaping (String) -> Void) in
            DispatchQueue.global().asyncAfter(deadline: .now() + 0.05) { answer("ask") }
        }
        XCTAssertEqual(later, "ask")
        XCTAssertEqual(FirstRunModel.firstAnswer(within: 1) { (answer: @escaping (String) -> Void) in answer("ok"); answer("off") }, "ok")
    }

    func testFirstAnswerIsNilAfterTimeoutAndALateAnswerGoesNowhere() {
        var late: ((String) -> Void)?
        XCTAssertNil(FirstRunModel.firstAnswer(within: 0.05) { (answer: @escaping (String) -> Void) in late = answer })
        late?("ok")     // 超时以后才到：不崩，也没人读
    }
}
