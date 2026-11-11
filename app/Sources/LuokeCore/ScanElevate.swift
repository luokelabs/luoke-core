// 扫描请本体弹密码框（扫描仓库 docs/host-interface.md §3–§5）。本体起的每个 `luoke-scan mcp` 的描述符 3 是这条通道
// （同一条通道上还有 reveal、open_settings，见 HostRequests.swift），
// 一行一条 JSON：扫描发 `{"id", "method": "elevate", "params": {"args", "prompt"}}`，本体答 done / cancelled / failed。
// 本体逐项核对参数，核过才起一个只做这一件事的子进程：螺壳自己（`--elevate-scan`），环境只有 PATH、工作目录 /、只有三个标准描述符
// （授权服务会把发起进程的环境交给 root 的 shell，shell 在第一条命令之前就读 SHELLOPTS、PS4 之类：本体是长驻进程，不能换掉自己的环境）。
// 子进程再核一遍参数，把环境清到只剩 PATH（CoreFoundation 启动时会自己加 __CF_USER_TEXT_ENCODING），拼 root 跑的那条命令
// （照扫描的 AdminCommand，internal/service/elevate.go），用 NSAppleScript 以管理员身份跑；框上的应用名是螺壳。
// root 那一侧把 luoke-scan-admin 拷进只有 root 能写的临时目录，核对签名（团队取自螺壳自己的签名，不写在源码里），再用空环境跑。
// luoke-scan-admin 是螺壳自己带的（Contents/Helpers），路径由子进程自己定，不从请求里拿。
import AppKit
import Darwin
import Foundation

enum ScanElevate {
    static let childFlag = "--elevate-scan"
    static let adminIdentifier = "ai.luoke.scan.admin"
    static let pathValue = "/usr/bin:/bin:/usr/sbin:/sbin"
    /// 同一时间只弹一个框（几个扫描进程同时要，排队）
    private static let one = NSLock()

    // MARK: 核对

    private static func matches(_ s: String, _ pattern: String) -> Bool {
        s.range(of: "^(?:\(pattern))$", options: .regularExpression) != nil
    }

    /// `args` 逐项核对（host-interface.md §3）：顺序固定 `--plan`、`--user`、`--keys`、`--apps`，最后可以有一个 `--homebrew`
    static func valid(_ args: [String], uid: uid_t = getuid()) -> Bool {
        guard args.count == 8 || (args.count == 9 && args[8] == "--homebrew") else { return false }
        guard args[0] == "--plan", args[2] == "--user", args[4] == "--keys", args[6] == "--apps" else { return false }
        guard matches(args[1], "p-[0-9a-f]{12}") else { return false }
        guard matches(args[3], "[0-9]{1,10}"), UInt32(args[3]) == uid else { return false }
        let keys = args[5].split(separator: ",", omittingEmptySubsequences: false).map(String.init)
        guard !keys.isEmpty, keys.allSatisfy({ matches($0, "a[0-9]{1,3}") }) else { return false }
        if args[7].isEmpty { return args.count == 9 }        // 没有应用的只在带 --homebrew 时
        let apps = args[7].split(separator: ",", omittingEmptySubsequences: false).map(String.init)
        return apps.allSatisfy { matches($0, "[A-Za-z0-9-]+(\\.[A-Za-z0-9-]+){2,}") }
    }

    static func quote(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }

    static func requirement(team: String) -> String {
        "=anchor apple generic and identifier \"\(adminIdentifier)\" and certificate leaf[subject.OU] = \"\(team)\""
            + " and certificate 1[field.1.2.840.113635.100.6.2.6] and certificate leaf[field.1.2.840.113635.100.6.1.13]"
    }

    /// root 跑的那条命令，和扫描的 AdminCommand 一字不差：root 自己的临时目录、拷进去（不经链接）、核文件和签名，再用空环境跑。
    /// 准备失败退 70、71，签名不对 72，那一步自己失败 73
    static func command(helper: String, args: [String], team: String) -> String {
        let copy = "\"$d/luoke-scan-admin\""
        let h = quote(helper)
        return [
            "umask 077",
            "d=$(/usr/bin/mktemp -d /private/var/tmp/luoke-scan-admin.XXXXXXXX) || exit 70",
            "trap '/bin/rm -rf \"$d\"' EXIT",
            "[ ! -L \(h) ] && [ -f \(h) ] && [ $(/usr/bin/stat -f %z \(h)) -lt 104857600 ] || exit 71",
            "( ulimit -f 204800; /bin/cp -X -P \(h) \(copy) ) || exit 71",
            "[ ! -L \(copy) ] && [ -f \(copy) ] && [ $(/usr/bin/stat -f %z \(copy)) -lt 104857600 ] || exit 71",
            "/usr/bin/codesign --verify --strict -R \(quote(requirement(team: team))) \(copy) || exit 72",
            "/usr/bin/env -i PATH=/usr/bin:/bin \(copy) \(args.map(quote).joined(separator: " ")) || exit 73",
        ].joined(separator: "; ")
    }

    // MARK: 本体这一侧

    /// 读一个工具进程的描述符 3，到它退出（读到结尾）为止。只有螺壳扫描能请弹框（elevate）；在访达里选中（reveal）、
    /// 打开系统设置的一页（open_settings）由本体的工具都能请（HostRequests.swift）。描述符由 ToolRun 关，这里读完 signal `done`
    static func listen(_ fd: Int32, tool: HostedTool, done: DispatchSemaphore, requests: HostRequests.Env = .live) {
        let t = Thread {
            let lines = FD.Lines(fd, limit: 1 << 16)
            while let line = lines.next() {
                guard let msg = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any], let id = msg["id"] else { continue }
                let params = msg["params"] as? [String: Any] ?? [:]
                let result: [String: Any]
                switch msg["method"] as? String {
                case "elevate":
                    result = tool.accessory == "luoke-scan" ? answer(params) : failed("这个工具不能请螺壳弹密码框")
                case "reveal":
                    result = HostRequests.reveal(params, env: requests)
                case "open_settings":
                    result = HostRequests.openSettings(params, env: requests)
                default:
                    result = failed("不认识的请求")
                }
                _ = FD.writeLine(fd, ["id": id, "result": result])
            }
            done.signal()
        }
        t.name = "tool-host-fd3"
        t.start()
    }

    static func failed(_ why: String) -> [String: Any] { ["status": "failed", "why": why] }

    /// 一次 elevate：核参数，起弹框的子进程，等它答。最多等 280 秒（扫描那边也只等这么久）
    static func answer(_ params: [String: Any]) -> [String: Any] {
        guard let args = params["args"] as? [String], valid(args) else { return failed("参数不对，没有动任何东西") }
        guard CodeSigning.selfTeam != nil else { return failed("开发版不能删系统目录里的东西") }
        guard let me = Bundle.main.executablePath else { return failed("删除程序没能准备好") }
        let prompt = String((params["prompt"] as? String ?? "").prefix(500))
        let request = FD.json(["args": args, "prompt": prompt])
        one.lock()
        defer { one.unlock() }
        guard let (input, inChild) = try? FD.pair(), let (output, outChild) = try? FD.pair() else { return failed("删除程序没能准备好") }
        let devnull = open("/dev/null", O_RDWR | O_CLOEXEC)
        let pid = try? Spawn.run(me, [childFlag], env: ["PATH=" + pathValue], cwd: "/", fds: [(inChild, 0), (outChild, 1), (devnull, 2)],
                               access: .luoke)     // 替扫描删受保护的东西，要螺壳的授权
        close(inChild); close(outChild); close(devnull)
        guard let pid else { close(input); close(output); return failed("删除程序没能准备好") }
        _ = FD.writeAll(input, request + Data([0x0a]))
        shutdown(input, SHUT_WR)
        close(input)
        var reply: Data?
        let read = DispatchSemaphore(value: 0)
        Thread { reply = FD.Lines(output, limit: 1 << 22).next(); read.signal() }.start()
        var timedOut = false
        if read.wait(timeout: .now() + 280) == .timedOut { timedOut = true; kill(pid, SIGKILL); read.wait() }   // 杀了它，读的那边读到结尾
        var status: Int32 = 0
        while waitpid(pid, &status, 0) < 0 && errno == EINTR {}
        close(output)
        if timedOut {
            // 框已经关了；但用户要是已经输了密码，root 那一步不跟着结束，可能做了一部分：让扫描自己再看一遍磁盘
            return ["status": "failed", "why": "等了 280 秒没有答复，可能已经做了一部分", "uncertain": true]
        }
        guard let reply, let o = (try? JSONSerialization.jsonObject(with: reply)) as? [String: Any], o["status"] is String else {
            return failed("要密码的那一步没有答复")
        }
        return o
    }

    // MARK: 子进程（螺壳 --elevate-scan）

    /// 弹框的子进程：不起界面。环境只该有本体给的 PATH（加 CoreFoundation 自己添的那一个），不是就不做；读一行请求，再核一遍，
    /// 环境清到只剩 PATH，弹框，写一行答复，退出
    static func child() -> Never {
        func reply(_ o: [String: Any]) -> Never {
            FileHandle.standardOutput.write(FD.json(o) + Data([0x0a]))
            exit(0)
        }
        guard environmentOK(currentEnvironment()), chdir("/") == 0, let helper = adminHelper() else { reply(failed("删除程序没能准备好")) }
        guard let line = FD.Lines(0, limit: 1 << 16).next(),
              let o = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any],
              let args = o["args"] as? [String], valid(args) else { reply(failed("参数不对，没有动任何东西")) }
        guard let team = CodeSigning.selfTeam else { reply(failed("开发版不能删系统目录里的东西")) }
        for name in currentEnvironment().keys where name != "PATH" { unsetenv(name) }
        guard currentEnvironment() == ["PATH": pathValue] else { reply(failed("删除程序没能准备好")) }   // 紧挨着弹框再核一次
        let prompt = String((o["prompt"] as? String ?? "").prefix(500))
        let script = "do shell script \(appleString(command(helper: helper, args: args, team: team)))"
            + " with prompt \(appleString(prompt)) with administrator privileges without altering line endings"
        var error: NSDictionary?
        let out = NSAppleScript(source: script)?.executeAndReturnError(&error)
        if let error {
            switch error[NSAppleScript.errorNumber] as? Int {
            case -128: reply(["status": "cancelled"])
            case 72: reply(failed("删除程序的签名不对，没有动任何东西"))
            case 73: reply(failed("删除那一步出错了"))
            case 70, 71: reply(failed("删除程序没能准备好"))
            default: reply(failed("要密码的那一步没成：\(error[NSAppleScript.errorMessage] as? String ?? "原因不明")"))
            }
        }
        reply(["status": "done", "output": out?.stringValue ?? ""])
    }

    /// 删除程序：螺壳自己带的那个（Contents/Helpers/luoke-scan-admin，是普通文件）。root 那一侧还会拷走再核签名
    static func adminHelper(helpers: URL = HostedTool.helpersDir) -> String? {
        let path = helpers.appendingPathComponent("luoke-scan-admin").path
        var st = stat()
        return lstat(path, &st) == 0 && (st.st_mode & S_IFMT) == S_IFREG ? path : nil
    }

    /// 本体起它时只给 PATH；CoreFoundation 启动时自己加 __CF_USER_TEXT_ENCODING，别的都不该有
    static func environmentOK(_ env: [String: String]) -> Bool {
        env["PATH"] == pathValue && Set(env.keys).isSubset(of: ["PATH", "__CF_USER_TEXT_ENCODING"])
    }

    /// 这一刻的环境（ProcessInfo 每次现读，unsetenv 之后就看不到了，11-05 实测）
    static func currentEnvironment() -> [String: String] { ProcessInfo.processInfo.environment }

    static func appleString(_ s: String) -> String {
        "\"" + s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") + "\""
    }
}
