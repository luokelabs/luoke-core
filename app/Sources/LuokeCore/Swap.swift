import Foundation

/// 换包（ADR 0012）：新版先在暂存处准备好、核对过签名，交给一个独立的 /bin/sh 小脚本；脚本等主控（或安装器）退出，
/// 把旧版挪进 previous.noindex、新版换上、登记，再用 /usr/bin/open 以用户身份打开新版。
/// - 不再由安装器以同一个 bundle id 的新实例接管：那样每一层都把上一层记成父进程，死图标挂在程序坞上（10-24，macOS 27）。
/// - 目标目录写不进去（标准账户的 /Applications）时，脚本经系统的管理员授权框以 root 跑；授权框在退出之前弹，取消了什么都不动。
/// 结果写在 run/swap-result.json，下一次启动的主控（新版，或换失败时放回的旧版）取走。
enum Swap {
    static var resultFile: URL { AppInfo.envDir.appendingPathComponent("run/swap-result.json") }
    static var scriptFile: URL { AppInfo.envDir.appendingPathComponent("run/swap.sh") }
    static var logFile: URL { AppInfo.envDir.appendingPathComponent("logs/swap.log") }

    /// 参数：等退出的 pid、已装的那份（没有就空）、暂存的新版、换到哪、旧版挪到哪、结果文件、用户 uid
    static let script = #"""
    #!/bin/sh
    # 螺壳换包（ADR 0012，app/Sources/LuokeCore/Swap.swift 写出来的）。旧版、新版都只用 mv：同一个盘上是改名，没有拷了一半的状态
    PID="$1"; CUR="$2"; STAGED="$3"; TARGET="$4"; PREV="$5"; RESULT="$6"; USERID="$7"
    LS=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
    say() {
      printf '{"ok": %s, "why": "%s", "app": "%s", "at": %s}\n' "$1" "$2" "$3" "$(date +%s)" > "$RESULT"
      [ "$(id -u)" = 0 ] && chown "$USERID" "$RESULT"
    }
    asuser() {    # root 跑的时候切回用户身份执行（LaunchServices 的登记库是每个用户各一份）
      if [ "$(id -u)" = 0 ]; then launchctl asuser "$USERID" sudo -u "#$USERID" "$@"; else "$@"; fi
    }
    lsr() { asuser "$LS" "$@"; }
    openapp() {   # 以用户身份打开（root 跑的时候切回用户），不带父进程
      if [ "$(id -u)" = 0 ]; then launchctl asuser "$USERID" sudo -u "#$USERID" /usr/bin/open -n "$1"; else /usr/bin/open -n "$1"; fi
    }
    echo "== $(date) swap pid=$PID cur=$CUR target=$TARGET uid=$(id -u)"
    n=0; while kill -0 "$PID" 2>/dev/null && [ $n -lt 600 ]; do sleep 0.2; n=$((n+1)); done   # 最多等 2 分钟
    if kill -0 "$PID" 2>/dev/null; then say false "旧版螺壳两分钟还没退出" "$CUR"; exit 1; fi
    mkdir -p "$(dirname "$PREV")"
    if [ -n "$CUR" ] && [ -e "$CUR" ]; then
      rm -rf "$PREV"
      if ! out=$(mv "$CUR" "$PREV" 2>&1); then say false "搬不动已装的螺壳（$(echo "$out" | tr -d '"\\' | tail -1)）" "$CUR"; openapp "$CUR"; exit 1; fi
      lsr -u "$PREV"
    fi
    EXTRA=""
    if [ -e "$TARGET" ]; then   # 目标位置上另有一份我们的（两处各装了一份）：先挪开，换成了再删（不是我们的，调用前就拦下了）
      EXTRA="$PREV.extra"; rm -rf "$EXTRA"; mv "$TARGET" "$EXTRA" && lsr -u "$EXTRA"
    fi
    if ! out=$(mv "$STAGED" "$TARGET" 2>&1); then
      [ -n "$EXTRA" ] && mv "$EXTRA" "$TARGET"
      if [ -n "$CUR" ] && mv "$PREV" "$CUR"; then lsr -f "$CUR"; fi
      say false "换不上新版（$(echo "$out" | tr -d '"\\' | tail -1)），旧版已放回" "$CUR"; [ -n "$CUR" ] && openapp "$CUR"; exit 1
    fi
    [ -n "$EXTRA" ] && rm -rf "$EXTRA"
    lsr -f "$TARGET"
    say true "" "$TARGET"
    openapp "$TARGET"
    echo "== $(date) done"
    """#

    /// 起换包脚本。返回 nil = 起来了（调用方接着退出）；否则是给用户看的原因（例如授权框点了取消），什么都没动。
    static func start(current: URL?, staged: URL, target: URL, waitPID: pid_t, admin: Bool) -> String? {
        let fm = FileManager.default
        try? fm.createDirectory(at: scriptFile.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? fm.createDirectory(at: logFile.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? fm.removeItem(at: resultFile)
        // 脚本可能以 root 跑：要写的目录和日志先以用户身份建好，免得建出 root 的东西，之后用户自己写不进去（10-25 核出）
        try? fm.createDirectory(at: Installer.previousDir, withIntermediateDirectories: true)
        if !fm.fileExists(atPath: logFile.path) { fm.createFile(atPath: logFile.path, contents: nil) }
        do { try script.write(to: scriptFile, atomically: true, encoding: .utf8) } catch { return "写不了换包脚本（\(error.localizedDescription)）" }
        let prev = Installer.previousDir.appendingPathComponent("\(AppInfo.appName).app")
        let args = [scriptFile.path, String(waitPID), current?.path ?? "", staged.path, target.path, prev.path, resultFile.path, String(getuid())]
        // nohup + &：脚本在后台等我们退出，起它的 sh（或 osascript）立刻回来
        func sq(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }
        let shellCmd = "/usr/bin/nohup /bin/sh " + args.map(sq).joined(separator: " ") + " >> " + sq(logFile.path) + " 2>&1 &"
        let p = Process()
        let err = Pipe()
        p.standardError = err
        p.standardOutput = FileHandle.nullDevice
        if admin {
            // 标准账户：交给系统的管理员授权框（取消了返回 -128）
            func asStr(_ s: String) -> String { "\"" + s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") + "\"" }
            p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
            p.arguments = ["-e", "do shell script \(asStr(shellCmd)) with prompt \(asStr("螺壳要把新版放进「应用程序」文件夹。")) with administrator privileges"]
        } else {
            p.executableURL = URL(fileURLWithPath: "/bin/sh")
            p.arguments = ["-c", shellCmd]
        }
        do { try p.run() } catch { return "起不了换包脚本（\(error.localizedDescription)）" }
        p.waitUntilExit()
        if p.terminationStatus == 0 { return nil }
        let msg = String(decoding: err.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        if msg.contains("-128") { return "没有授权，新版没装上。原来的螺壳照常能用。" }
        return "没能起换包脚本：\(msg.trimmingCharacters(in: .whitespacesAndNewlines))"
    }

    /// 启动时取走上一次换包的结果（取完就删）：nil = 没换过包；(true, _) = 成了；(false, 原因) = 没成，现在跑的是放回的旧版
    static func takeResult() -> (ok: Bool, why: String)? {
        guard let d = FileManager.default.contents(atPath: resultFile.path),
              let o = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] else { return nil }
        try? FileManager.default.removeItem(at: resultFile)
        return (o["ok"] as? Bool ?? false, o["why"] as? String ?? "")
    }
}
