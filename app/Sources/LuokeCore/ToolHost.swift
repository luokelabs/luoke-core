// 由本体起的工具（luoke-agent 的工具协议 §2a、界面接口 §15）：不常驻、不开 socket，由本体当子进程起，权限算在螺壳名下。
// agent 核心的标准输入是本体给的一对 socketpair 的一端（`serve --host-stdin`）。核心在上面要连接（一行 `{"id": N, "open": "<配件>"}`）；
// 本体起一次工具，核过签名再放它跑，把连到它标准输入输出的那一端经 SCM_RIGHTS 交过去。每个请求一条连接：核心写一行请求、
// 关掉写的一头，工具答一行、读到结尾退出。
// 工具打在螺壳.app 里（顶层 ADR 0022 的 11-05 修订）：程序在 Contents/Helpers，跟螺壳一起升级、一起签名。螺壳可能装在用户能写的地方
// （~/Applications），所以照旧：程序和参数写死在这里；停着起（POSIX_SPAWN_START_SUSPENDED），核真正在跑的那个进程（螺壳自己的团队、
// Developer ID、写死的标识、和螺壳 Info.plist 里记的版本一致），核完再确认它还停着、一行没跑过，才放行；不过就结束它。
// 用户在「应用」里开启了才起（state.json 的 enabled，和主控同一个算法）。
// 螺壳搜索是常驻的，不走这里的连接，只用同一套核对（HelperProcess.check、Spawn.capture）；核心在这条通道上要它的口令
// （`{"id": N, "token": "luoke-search"}` → `{"id": N, "token": "…"}`，SearchHelper.token）。
// 先是螺壳扫描（扫描仓库的 docs/host-interface.md）：`luoke-scan mcp --this-mac`，描述符 3 是它请本体代做事的通道：弹密码框（ScanElevate.swift）、在访达里选中和打开系统设置（HostRequests.swift）。
// 螺壳记忆（V1 §6，记忆的清单 `commands.mcp`）：本体自带的 Python 跑 `-m luoke_mem --path <记忆文件> mcp`，记忆文件是本体交给 agent 核心的
// 那一份（AgentProcess.memory）。它没有自己的签名标识和版本：核整个螺壳的签名（里面的 Python 代码和记忆的模块），再核这个进程跑的就是那份 Python。
import Darwin
import Foundation
import Security

/// 本体和 agent 核心之间的通道。核心每起一次建一个，核心退出就 stop。
final class ToolHost {
    /// 起核心时当它的标准输入；起好以后调 `childStarted()`，本体这边的副本就关掉
    let agentEnd: FileHandle
    private let hostFD: Int32
    private let find: (String) -> HostedTool?
    private let enabled: (String) -> Bool
    private let check: (pid_t, HostedTool) -> String?
    private let token: (String) -> String?
    private let lock = NSLock()
    private var running = 0
    /// hostFD 只由 serve 关；stop 在锁里看这个，不去动关掉以后被别处重用的描述符号
    private var closed = false
    /// 同时在跑的工具进程最多几个（核心那边同一时间也就几个请求：读 outbox、点按、读页面）
    static let maxRunning = 4

    init?(find: @escaping (String) -> HostedTool? = { HostedTool.find($0) },
          enabled: @escaping (String) -> Bool = { HostedTool.enabled($0) },
          check: @escaping (pid_t, HostedTool) -> String? = { CodeSigning.checkRunning(pid: $0, tool: $1) },
          token: @escaping (String) -> String? = { $0 == SearchHelper.accessory && SearchHelper.enabled ? SearchHelper.currentToken : nil }) {
        var fds: [Int32] = [-1, -1]
        guard socketpair(AF_UNIX, SOCK_STREAM, 0, &fds) == 0 else { return nil }
        fds.forEach(FD.noSigPipe)
        FD.closeOnExec(fds[1])                   // 本体这一端不漏给任何子进程
        agentEnd = FileHandle(fileDescriptor: fds[0], closeOnDealloc: true)
        hostFD = fds[1]
        self.find = find
        self.enabled = enabled
        self.check = check
        self.token = token
    }

    /// 核心起好了：关掉本体手里的那一端副本，开始听它要连接
    func childStarted() {
        try? agentEnd.close()
        let t = Thread { [self] in serve() }
        t.name = "tool-host"
        t.start()
    }

    /// 核心退出了或本体要退：通道关掉，听的那边结束。已经起的工具照常答完、退出
    func stop() {
        lock.lock(); defer { lock.unlock() }
        if !closed { shutdown(hostFD, SHUT_RDWR) }
    }

    private func serve() {
        let lines = FD.Lines(hostFD, limit: 1 << 16)
        while let line = lines.next() {
            guard let msg = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any], let id = msg["id"] else { continue }
            if let name = msg["token"] as? String {
                // 常驻工具的口令（现在只有搜索）。答复不带 ok：和 open 的答复分得开，核心不会把随 open 来的连接配给它
                if let t = token(name) { _ = FD.writeLine(hostFD, ["id": id, "token": t]) }
                else { _ = FD.writeLine(hostFD, ["id": id, "error": "「\(name)」没有口令（没带、没开启或还没起来）"]) }
                continue
            }
            guard let name = msg["open"] as? String else {
                _ = FD.writeLine(hostFD, ["id": id, "error": "不认识的请求"])
                continue
            }
            switch open(name) {
            case .failure(let why):
                _ = FD.writeLine(hostFD, ["id": id, "error": why.text])
            case .success(let run):
                if !FD.send(hostFD, line: ["id": id, "ok": true], passing: run.agentSide) {
                    run.abandon()                // 核心不在了：工具读到结尾自己退
                }
            }
        }
        lock.lock(); closed = true; close(hostFD); lock.unlock()
    }

    private func open(_ accessory: String) -> Result<ToolRun, HostError> {
        guard let tool = find(accessory), tool.onDemand else { return .failure(HostError("螺壳没有带能这样起的「\(accessory)」")) }
        guard tool.installed || tool.always || enabled(accessory) else { return .failure(HostError("「\(accessory)」没有开启，要先在「应用」里开启")) }
        lock.lock()
        guard running < Self.maxRunning else {
            lock.unlock()
            return .failure(HostError("同时开着的连接太多了，过一会儿再试"))
        }
        running += 1
        lock.unlock()
        let done = { [weak self] in
            guard let self else { return }
            self.lock.lock(); self.running -= 1; self.lock.unlock()
        }
        do {
            let run = try tool.start(check: check)
            run.watch(timeout: tool.timeout, exited: done)
            return .success(run)
        } catch let e as HostError {
            done()
            return .failure(e)
        } catch {
            done()
            NSLog("%@ 起不来：%@", tool.accessory, "\(error)")
            return .failure(HostError("「\(tool.accessory)」起不来"))
        }
    }
}

struct HostError: Error {
    let text: String
    init(_ text: String) { self.text = text }
}

/// 一个由本体起的工具
struct HostedTool {
    /// 能这样起的工具：程序的签名标识、程序名、参数，都写死在这里。`versionKey`：螺壳 Info.plist 里记它版本的键（打包时写，build_app.sh）。
    /// `jobs`：主控跑空闲任务时（`螺壳 --run-tool`）能用的完整参数，一字不差（和清单 jobs 的 command 一样，清单在螺壳的签名里）
    struct Known {
        let identifier: String
        let program: String
        let arguments: [String]
        var versionKey: String? = nil
        var jobs: [[String]] = []
        /// 核心要连接、主控跑任务时按次起。常驻的（搜索）只借这里的核对，不走这两条路
        var onDemand = true
        /// 不在螺壳.app 里、是用户另装的配件（螺壳输入法，在 ~/Library/Input Methods）：程序在那个 .app 的 Contents/MacOS 里，
        /// 按已装配件的清单找；版本不归螺壳管，不核；装了就算开着
        var installed = false
        /// 程序是螺壳自带的 Python（Resources/python/bin 里的 `program`），跑本体 site-packages 里的模块（螺壳记忆）。
        /// 参数里的 `{memory}` 由本体换成它交给 agent 核心的那个记忆文件。核的见 CodeSigning.checkRunningPython
        var python = false
        /// 本体的一部分，用户关不掉（螺壳记忆，V1 §6）：不看「应用」里开没开
        var always = false
        /// 给不给描述符 3（请本体代做事：弹密码框、在访达里选中、打开系统设置）
        var hostChannel = true
        /// 借不借螺壳的系统授权（Spawn.swift）
        var access: Spawn.Access = .luoke
        var timeout: TimeInterval = 300
    }
    static let known: [String: Known] = [
        // 扫描的空闲任务：更新病毒库、整轮、两轮之间看一眼新下载的（`--quick`：现在合不合适、有没有新的由扫描自己定，不合适就马上退出）
        "luoke-scan": Known(identifier: "ai.luoke.scan", program: "luoke-scan", arguments: ["mcp", "--this-mac"], versionKey: "LuokeScanVersion",
                            jobs: [["update", "--this-mac"], ["scan", "--this-mac", "--autoclean"], ["scan", "--this-mac", "--quick"]]),
        "luoke-search": Known(identifier: "ai.luoke.search", program: "luoke-search", arguments: [], versionKey: "LuokeSearchVersion", onDemand: false),
        // 输入法的设置页（luoke-ime#29，负责人 11-09 定画进主窗口）：`luoke-ime mcp` 只读写 ~/.luoke/ime/settings.json，
        // 不要螺壳的授权，也不请本体代做事
        "luoke-ime": Known(identifier: "luoke-ime", program: "luoke-ime", arguments: ["mcp"], installed: true, hostChannel: false,
                           access: .disclaimed, timeout: 30),
        // 螺壳记忆：清单 `commands.mcp`（`-m luoke_mem --path {memory} mcp`）前面加 -I（不认 PYTHON* 环境变量、用户的 site-packages
        // 和工作目录）、-B（不写 .pyc）。只读写记忆文件，不要螺壳的授权，也不请本体代做事。忘掉某个应用写的、清空整份记忆可能要一会儿
        "luoke-mem": Known(identifier: "luoke-mem", program: "python3",
                           arguments: ["-I", "-B", "-m", "luoke_mem", "--path", "{memory}", "mcp"],
                           python: true, always: true, hostChannel: false, access: .disclaimed, timeout: 120),
    ]

    /// 工具进程的环境只有这几样（扫描仓库 docs/host-interface.md §2）；开发版、实验室加上自己主控的 socket
    static func environment(_ accessory: String) -> [String] {
        var env = ["HOME=\(NSHomeDirectory())", "TMPDIR=\(FD.userTempDir())", "PATH=/usr/bin:/bin:/usr/sbin:/sbin", "LANG=en_US.UTF-8"]
        if BundledApps.developmentBuild { env.append("LUOKE_HUB_SOCKET=\(AppInfo.socketPath)") }
        if let off = BundledApps.offVariable(accessory) { env.append(off) }
        return env
    }

    let accessory: String
    let identifier: String
    /// 螺壳打包时记下的它的版本；起来以后和它签名里的版本比。开发版没记是 nil
    let version: String?
    /// 程序所在的目录（螺壳.app/Contents/Helpers）
    let dir: URL
    let program: URL
    let arguments: [String]
    var onDemand = true
    var installed = false
    var python = false
    var always = false
    var hostChannel = true
    var access: Spawn.Access = .luoke
    /// 一个请求最长多久（扫描：执行方案时核对最多 2 分钟，再加上用户在密码框前的时间）
    var timeout: TimeInterval = 300
    /// 它的标准错误写到哪（没给就是本体日志目录下的 <配件>.log）
    var logFile: URL? = nil

    static var helpersDir: URL { Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers", isDirectory: true) }
    /// 自带 Python 的程序目录
    static var pythonDir: URL { AppInfo.resources.appendingPathComponent("python/bin", isDirectory: true) }

    /// 写死了的、螺壳带着的（程序是个普通文件，不是链接）才找得到。开发版没带（没给 LUOKE_SCAN_SRC）就是 nil。
    /// 用户另装的（`installed`）在已装配件清单给的 .app 里找（`installedApp`）。
    /// 跑在自带 Python 上的（`python`）在 `pythonDir` 里找：python3 是指到同一个目录里真正程序的链接，起的是链接指的那个；
    /// 参数里的 `{memory}` 换成 `memory`（本体交给 agent 核心的记忆文件），本体没带记忆（Memory.bundled）就是 nil
    static func find(_ accessory: String, helpers: URL = helpersDir, info: [String: Any]? = Bundle.main.infoDictionary,
                     known: [String: Known] = known,
                     installedApp: (String) -> URL? = { id in Accessories.installed().first { $0.accessory == id && !$0.bundled }?.app },
                     python: URL = pythonDir, memory: () -> String? = { Memory.bundled ? AgentProcess.memory.path : nil }) -> HostedTool? {
        guard let k = known[accessory] else { return nil }
        let dir: URL
        var arguments = k.arguments
        if k.installed {
            guard let app = installedApp(accessory) else { return nil }
            dir = app.appendingPathComponent("Contents/MacOS", isDirectory: true)
        } else if k.python {
            dir = python
            if arguments.contains("{memory}") {
                guard let m = memory(), m.hasPrefix("/") else { return nil }
                arguments = arguments.map { $0 == "{memory}" ? m : $0 }
            }
        } else {
            dir = helpers
        }
        var program = dir.appendingPathComponent(k.program)
        if k.python {
            // 链接只认指到同一个目录里的（python3 → python3.12）
            let real = program.resolvingSymlinksInPath()
            guard real.deletingLastPathComponent().path == dir.resolvingSymlinksInPath().path else { return nil }
            program = real
        }
        var st = stat()
        guard lstat(program.path, &st) == 0, (st.st_mode & S_IFMT) == S_IFREG, Darwin.access(program.path, X_OK) == 0 else { return nil }
        return HostedTool(accessory: accessory, identifier: k.identifier,
                          version: k.installed || k.python ? nil : k.versionKey.flatMap { info?[$0] as? String },
                          dir: dir, program: program, arguments: arguments, onDemand: k.onDemand, installed: k.installed,
                          python: k.python, always: k.always, hostChannel: k.hostChannel, access: k.access, timeout: k.timeout)
    }

    /// 这个进程是不是这个工具：临时起的和常驻的都用它（`check` 是给测试的）
    static func check(_ accessory: String, pid: pid_t, find: (String) -> HostedTool? = { HostedTool.find($0) },
                      check: (pid_t, HostedTool) -> String? = { CodeSigning.checkRunning(pid: $0, tool: $1) }) -> String? {
        guard let tool = find(accessory) else { return "螺壳没有带「\(accessory)」" }
        return check(pid, tool)
    }

    /// 用户开启了没有
    static func enabled(_ accessory: String) -> Bool { BundledApps.enabled(accessory) }

    /// 停着起：标准输入输出接 `conn`（交给核心的那条连接的另一端），标准错误进本体的日志，描述符 3 是它请本体代做事的通道（弹密码框、在访达里选中、打开系统设置）；
    /// 环境只给这几样，工作目录 /，别的描述符一个不漏（扫描仓库 docs/host-interface.md §2）。核过、确认没被提前放出来才放行
    func start(check: (pid_t, HostedTool) -> String?) throws -> ToolRun {
        let (agentSide, toolSide) = try FD.pair()
        let (hostSide, toolHost): (Int32, Int32)
        do { (hostSide, toolHost) = try FD.pair() } catch { close(agentSide); close(toolSide); throw error }
        FD.closeOnExec(agentSide)
        FD.closeOnExec(hostSide)
        let log = FD.appendLog(logFile ?? AppInfo.envDir.appendingPathComponent("logs/\(accessory).log"))
        defer {
            close(toolSide)
            close(toolHost)
            if log >= 0 { close(log) }
        }
        let env = HostedTool.environment(accessory) + (hostChannel ? ["LUOKE_HOST_FD=3"] : [])
        var fds = [(toolSide, Int32(0)), (toolSide, 1), (log >= 0 ? log : toolSide, 2)]
        if hostChannel { fds.append((toolHost, 3)) }
        let pid: pid_t
        do {
            pid = try Spawn.run(program.path, arguments, env: env, cwd: "/", suspended: true, fds: fds,
                                access: access)     // 扫描要用螺壳的授权（Spawn.swift）；输入法不借
        } catch {
            close(agentSide); close(hostSide)
            throw error
        }
        if let why = Spawn.release(pid, check: { check(pid, self) }) {
            close(agentSide); close(hostSide)
            throw HostError(why)
        }
        let run = ToolRun(pid: pid, agentSide: agentSide, hostSide: hostSide)
        if hostChannel {
            ScanElevate.listen(hostSide, tool: self, done: run.listenerDone)   // 描述符 3：弹框只给扫描；在访达里选中、打开系统设置都能请
        } else {
            run.listenerDone.signal()           // 没给描述符 3：没有要等的
        }
        return run
    }
}

/// 一次工具进程。本体留着交给核心的那一端，到工具退出再关：交出去马上关，macOS 可能把还在路上的 socket 当没人要的收掉，
/// 核心拿到的就是断的（11-05 测过：马上关 960 次断 14 次，留着 3840 次一次没断）。
/// 每个描述符只由这里关一次；关了以后别处不再碰那个号（可能已经被并发的请求重用）
final class ToolRun {
    let agentSide: Int32
    private let hostSide: Int32
    private let pid: pid_t
    private let lock = NSLock()
    private var done = false
    private var agentClosed = false
    /// 提权通道那边读完了（ScanElevate.listen 不关描述符，读到结尾就 signal）
    let listenerDone = DispatchSemaphore(value: 0)

    init(pid: pid_t, agentSide: Int32, hostSide: Int32) {
        self.pid = pid
        self.agentSide = agentSide
        self.hostSide = hostSide
    }

    /// 核心没拿到这条连接：两个方向都关掉，工具读到结尾自己退
    func abandon() {
        lock.lock(); defer { lock.unlock() }
        if !agentClosed { shutdown(agentSide, SHUT_RDWR) }
    }

    /// 等它退出、收尸、关掉本体留着的两端；超过 timeout 还没退就结束它。
    /// 先等它退出但不收（WNOWAIT），在锁里记下「退了」再收：收掉以前 pid 不会被重用，超时那边在同一把锁里看，不会杀错进程
    func watch(timeout: TimeInterval, exited: @escaping () -> Void) {
        let t = Thread { [self] in
            var info = siginfo_t()
            while waitid(P_PID, id_t(pid), &info, WEXITED | WNOWAIT) < 0 && errno == EINTR {}
            lock.lock(); done = true; lock.unlock()
            var status: Int32 = 0
            while waitpid(pid, &status, 0) < 0 && errno == EINTR {}
            lock.lock()
            agentClosed = true
            shutdown(agentSide, SHUT_RDWR)
            close(agentSide)
            lock.unlock()
            shutdown(hostSide, SHUT_RDWR)       // 提权那边在读它：读到结尾就停
            exited()
            listenerDone.wait()                 // 它可能还在等用户输密码；答完再关
            close(hostSide)
        }
        t.name = "tool-run-\(pid)"
        t.start()
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout) { [self] in
            lock.lock(); defer { lock.unlock() }
            if !done { kill(pid, SIGKILL) }
        }
    }
}

/// 代码签名：螺壳自己的团队，和起来以后对那个进程的核对
enum CodeSigning {
    /// 螺壳自己签名里的团队（Developer ID）；临时签名的（开发版、实验室构建）没有
    static let selfTeam: String? = {
        var code: SecCode?
        guard SecCodeCopySelf(SecCSFlags(), &code) == errSecSuccess, let code else { return nil }
        var s: SecStaticCode?
        guard SecCodeCopyStaticCode(code, SecCSFlags(), &s) == errSecSuccess, let s else { return nil }
        var info: CFDictionary?
        guard SecCodeCopySigningInformation(s, SecCSFlags(rawValue: kSecCSSigningInformation), &info) == errSecSuccess,
              let dict = info as? [String: Any] else { return nil }
        return dict[kSecCodeInfoTeamIdentifier as String] as? String
    }()

    /// Developer ID 签的、螺壳的团队、写死的标识（Apple Development 证书签的开发版不算）；`identifier` 是 nil 时不限标识（自带的 Python）
    static func requirement(identifier: String?, team: String) -> String {
        "anchor apple generic" + (identifier.map { " and identifier \"\($0)\"" } ?? "") + " and certificate leaf[subject.OU] = \"\(team)\""
            + " and certificate 1[field.1.2.840.113635.100.6.2.6] and certificate leaf[field.1.2.840.113635.100.6.1.13]"
    }

    /// 签名里的版本（编进程序的 Info.plist，签名盖着它）和螺壳打包时记下的一样
    static func versionMatches(_ plist: [String: Any]?, _ want: String) -> Bool {
        plist?["CFBundleShortVersionString"] as? String == want
    }

    /// 螺壳整个包的签名：Python 的程序没法逐个核，起主控、agent 核心和邮件助手之前核一次整包。
    /// 过了是 nil。临时签名（开发版、实验室构建）不核。一次启动只核一次：读完整个包要一点时间
    static let bundleProblem: String? = {
        guard let team = selfTeam else { return BundledApps.developmentBuild ? nil : "螺壳自己的签名读不到" }
        var s: SecStaticCode?
        var req: SecRequirement?
        guard SecStaticCodeCreateWithPath(Bundle.main.bundleURL as CFURL, SecCSFlags(), &s) == errSecSuccess, let s,
              SecRequirementCreateWithString("anchor apple generic and certificate leaf[subject.OU] = \"\(team)\"" as CFString, SecCSFlags(), &req) == errSecSuccess,
              let req else { return "读不到螺壳的签名" }
        let flags = SecCSFlags(rawValue: kSecCSCheckAllArchitectures | kSecCSStrictValidate | kSecCSCheckNestedCode)
        return SecStaticCodeCheckValidity(s, flags, req) == errSecSuccess ? nil : "螺壳的文件被改过"
    }()

    /// 停着的子进程（本体起的、还没收的，pid 不会被重用）：核它真正在跑的代码，过了是 nil。
    /// 螺壳自己是临时签名（开发版、实验室构建）时不核；正式版读不到自己的团队、没记它的版本就不放行
    static func checkRunning(pid: pid_t, tool: HostedTool) -> String? {
        guard let team = selfTeam else {
            return BundledApps.developmentBuild ? nil : "螺壳自己的签名读不到，不起它"
        }
        if tool.python { return checkRunningPython(pid: pid, tool: tool, team: team) }
        var code: SecCode?
        var req: SecRequirement?
        let attrs = [kSecGuestAttributePid: NSNumber(value: pid)] as CFDictionary
        guard SecCodeCopyGuestWithAttributes(nil, attrs, SecCSFlags(), &code) == errSecSuccess, let code,
              SecRequirementCreateWithString(requirement(identifier: tool.identifier, team: team) as CFString, SecCSFlags(), &req) == errSecSuccess,
              let req else { return "读不到它的签名，不起它" }
        guard SecCodeCheckValidity(code, SecCSFlags(), req) == errSecSuccess else {
            return "它的签名不对，不起它"
        }
        if tool.installed { return nil }        // 用户另装的：签名标识和团队对就行，版本不归螺壳管
        guard let want = tool.version else { return "螺壳没记它的版本，不起它" }
        var s: SecStaticCode?
        var info: CFDictionary?
        guard SecCodeCopyStaticCode(code, SecCSFlags(), &s) == errSecSuccess, let s,
              SecCodeCopySigningInformation(s, SecCSFlags(rawValue: kSecCSSigningInformation), &info) == errSecSuccess,
              let dict = info as? [String: Any] else { return "读不到它的版本，不起它" }
        return versionMatches(dict[kSecCodeInfoPList as String] as? [String: Any], want) ? nil : "它不是螺壳带的那一版，不起它"
    }

    /// 跑在自带 Python 上的工具（螺壳记忆）：Python 的代码没法逐个核，先核整个螺壳的签名（`bundleProblem`，里面的 .py 都盖在
    /// 包的签名里），再核停着的这个进程：螺壳的团队、Developer ID 签的，跑的程序就是 `tool.program`（自带的那份 Python）
    static func checkRunningPython(pid: pid_t, tool: HostedTool, team: String, bundle: () -> String? = { bundleProblem }) -> String? {
        if let why = bundle() { return why + "，不起它" }
        var code: SecCode?
        var req: SecRequirement?
        let attrs = [kSecGuestAttributePid: NSNumber(value: pid)] as CFDictionary
        guard SecCodeCopyGuestWithAttributes(nil, attrs, SecCSFlags(), &code) == errSecSuccess, let code,
              SecRequirementCreateWithString(requirement(identifier: nil, team: team) as CFString, SecCSFlags(), &req) == errSecSuccess,
              let req else { return "读不到它的签名，不起它" }
        guard SecCodeCheckValidity(code, SecCSFlags(), req) == errSecSuccess else { return "它的签名不对，不起它" }
        var s: SecStaticCode?
        var path: CFURL?
        guard SecCodeCopyStaticCode(code, SecCSFlags(), &s) == errSecSuccess, let s,
              SecCodeCopyPath(s, SecCSFlags(), &path) == errSecSuccess, let path else { return "读不到它在哪，不起它" }
        return samePython(path as URL, tool.program) ? nil : "它不是螺壳带的 Python，不起它"
    }

    /// 进程跑的程序和自带的那份 Python 是同一个文件
    static func samePython(_ running: URL, _ program: URL) -> Bool {
        running.resolvingSymlinksInPath().standardizedFileURL.path == program.resolvingSymlinksInPath().standardizedFileURL.path
    }
}

/// 描述符的小工具：socketpair、一行一行地读、连同描述符一起发
enum FD {
    static func pair() throws -> (Int32, Int32) {
        var fds: [Int32] = [-1, -1]
        guard socketpair(AF_UNIX, SOCK_STREAM, 0, &fds) == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        fds.forEach(noSigPipe)
        return (fds[0], fds[1])
    }

    static func noSigPipe(_ fd: Int32) {
        var one: Int32 = 1
        _ = setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
    }

    static func closeOnExec(_ fd: Int32) { _ = fcntl(fd, F_SETFD, FD_CLOEXEC) }

    /// 这个用户的临时目录（/var/folders/…/T/）
    static func userTempDir() -> String {
        var buf = [CChar](repeating: 0, count: Int(PATH_MAX))
        let n = confstr(_CS_DARWIN_USER_TEMP_DIR, &buf, buf.count)
        return n > 0 ? String(cString: buf) : NSTemporaryDirectory()
    }

    /// 日志最大多少：超过就挪成 .1（旧的 .1 丢掉）再写新的
    static let logLimit: Int64 = 1 << 20

    /// 追加写的日志（600）；打不开是 -1
    static func appendLog(_ url: URL) -> Int32 {
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        var st = stat()
        if lstat(url.path, &st) == 0, Int64(st.st_size) > logLimit {
            _ = rename(url.path, url.path + ".1")
        }
        return open(url.path, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0o600)
    }

    static func json(_ object: [String: Any]) -> Data {
        (try? JSONSerialization.data(withJSONObject: object)) ?? Data("{}".utf8)
    }

    static func writeAll(_ fd: Int32, _ data: Data) -> Bool {
        var rest = data[...]
        while !rest.isEmpty {
            let n = rest.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
            if n < 0 { if errno == EINTR { continue }; return false }
            rest = rest.dropFirst(n)
        }
        return true
    }

    static func writeLine(_ fd: Int32, _ object: [String: Any]) -> Bool { writeAll(fd, json(object) + Data([0x0a])) }

    /// 一行（JSON 加换行）连同描述符 `pass` 一起发（SCM_RIGHTS）
    static func send(_ sock: Int32, line object: [String: Any], passing pass: Int32) -> Bool {
        var bytes = [UInt8](json(object) + Data([0x0a]))
        let header = MemoryLayout<cmsghdr>.size                 // Darwin：12，按 4 字节对齐不用补
        let space = header + MemoryLayout<Int32>.size           // CMSG_SPACE(sizeof(int)) == CMSG_LEN(sizeof(int)) == 16
        var control = [UInt8](repeating: 0, count: space)
        let sent: Int = bytes.withUnsafeMutableBytes { b in
            control.withUnsafeMutableBytes { c in
                let cmsg = c.baseAddress!.assumingMemoryBound(to: cmsghdr.self)
                cmsg.pointee.cmsg_len = socklen_t(space)
                cmsg.pointee.cmsg_level = SOL_SOCKET
                cmsg.pointee.cmsg_type = SCM_RIGHTS
                (c.baseAddress! + header).storeBytes(of: pass, as: Int32.self)
                var iov = iovec(iov_base: b.baseAddress, iov_len: b.count)
                return withUnsafeMutablePointer(to: &iov) { iovp in
                    var msg = msghdr(msg_name: nil, msg_namelen: 0, msg_iov: iovp, msg_iovlen: 1,
                                     msg_control: c.baseAddress, msg_controllen: socklen_t(space), msg_flags: 0)
                    var n: Int
                    repeat { n = sendmsg(sock, &msg, 0) } while n < 0 && errno == EINTR
                    return n
                }
            }
        }
        if sent == bytes.count { return true }
        if sent <= 0 { return false }
        return writeAll(sock, Data(bytes[sent...]))              // 描述符已经随头一段到了，剩下的照常写
    }

    /// 另开一个线程读 fd：每读到一行带 id 的请求，答一行 `{"id": …, "result": answer(请求)}`，读到结尾停。
    /// 本体起的工具启动时在描述符 3 上发一行 hello、等答复，认出答话的是它的父进程（是螺壳起的）。
    /// 返回的信号量在停了以后 signal；描述符由调用方关（先 shutdown，读的这边就停）
    static func answerAll(_ fd: Int32, name: String, answer: @escaping ([String: Any]) -> [String: Any]) -> DispatchSemaphore {
        let done = DispatchSemaphore(value: 0)
        let t = Thread {
            let lines = Lines(fd, limit: 1 << 16)
            while let line = lines.next() {
                guard let msg = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any], let id = msg["id"] else { continue }
                _ = writeLine(fd, ["id": id, "result": answer(msg)])
            }
            done.signal()
        }
        t.name = name
        t.start()
        return done
    }

    /// 一行一行地读一个描述符（不含换行）。读到结尾、出错、一行超过 limit 都是 nil
    final class Lines {
        private let fd: Int32
        private let limit: Int
        private var buf = Data()
        private var eof = false

        init(_ fd: Int32, limit: Int) {
            self.fd = fd
            self.limit = limit
        }

        func next() -> Data? {
            while true {
                if let i = buf.firstIndex(of: 0x0a) {
                    let line = Data(buf[buf.startIndex..<i])
                    buf = Data(buf[(i + 1)...])
                    return line
                }
                if eof || buf.count > limit { return nil }
                var chunk = [UInt8](repeating: 0, count: 65536)
                let n = read(fd, &chunk, chunk.count)
                if n < 0 && errno == EINTR { continue }
                if n <= 0 { eof = true; continue }
                buf.append(contentsOf: chunk[0..<n])
            }
        }
    }
}
