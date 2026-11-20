import Foundation

/// 换包（ADR 0012）：新版先在暂存处准备好、核对过签名，交给一个独立的 /bin/sh 小脚本；脚本等主控（或安装器）退出，
/// 把旧版挪开、新版换上、登记，再用 /usr/bin/open 以用户身份打开新版。
/// - 不再由安装器以同一个 bundle id 的新实例接管：那样每一层都把上一层记成父进程，死图标挂在程序坞上（10-24，macOS 27）。
/// - 目标目录写不进去（标准账户的 /Applications）时，脚本经系统的管理员授权框以 root 跑；授权框在退出之前弹，取消了什么都不动。
/// - root 跑的时候不碰用户能改的东西（beta 2 安全审查 M1：之前 root 跑的是用户目录里的 run/swap.sh，换上的是用户目录里的暂存版，
///   同账户的程序在等旧版退出的那两分钟里改掉它们就拿到了 root）：
///   脚本不落盘，整段放在交给授权框的命令里；授权框的环境只给 PATH；新版先拷进只有 root 能进的临时目录，按螺壳自己的 Team 和
///   bundle id 核过签名，换上的是那一份；结果、日志、用户目录里的旧版都以用户身份写、挪。
/// 结果写在 run/swap-result.json，下一次启动的主控（新版，或换失败时放回的旧版）取走。
enum Swap {
    static var resultFile: URL { AppInfo.envDir.appendingPathComponent("run/swap-result.json") }
    static var logFile: URL { AppInfo.envDir.appendingPathComponent("logs/swap.log") }
    /// 以前的版本把脚本写在这里再交给 root 跑；现在不写了，起换包时顺手删掉留下的
    static var oldScriptFile: URL { AppInfo.envDir.appendingPathComponent("run/swap.sh") }
    static let pathValue = "/usr/bin:/bin:/usr/sbin:/sbin"

    /// 参数：等退出的 pid、已装的那份（没有就空）、暂存的新版、换到哪、旧版挪到哪、结果文件、用户 uid、
    /// 核新版签名的要求、root 自己的临时目录（只在 root 跑时有）、日志
    static let script = #"""
    # 螺壳换包（ADR 0012，app/Sources/LuokeCore/Swap.swift 拼进命令里交给 sh -c，不落盘）。旧版、新版都只用 mv：同一个盘上是改名
    PID="$1"; CUR="$2"; STAGED="$3"; TARGET="$4"; PREV="$5"; RESULT="$6"; USERID="$7"; REQ="$8"; WORK="$9"; LOG="${10}"
    trap '' HUP   # 不用 nohup（macOS 27 上授权框起的 root 进程里它报 can't detach from console，脚本一行不跑）：自己不理挂断
    [ -z "$WORK" ] || : > "$WORK/started" || exit 1    # 告诉起它的那条命令：起来了（没起来那边会报错，旧版不退出）
    LS=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
    OPEN=/usr/bin/open
    ROOT=0; [ "$(/usr/bin/id -u)" = 0 ] && ROOT=1
    asuser() {    # root 跑的时候切回用户身份执行：LaunchServices 的登记库每个用户各一份；用户目录里的东西也只以用户身份动
      if [ $ROOT = 1 ]; then /bin/launchctl asuser "$USERID" /usr/bin/sudo -u "#$USERID" "$@"; else "$@"; fi
    }
    say() {       # 结果文件在用户的目录里，以用户身份写：root 写会跟着用户放的链接写到系统文件上
      printf '{"ok": %s, "why": "%s", "app": "%s", "at": %s}\n' "$1" "$2" "$3" "$(/bin/date +%s)" | asuser /bin/sh -c 'cat > "$1"' say "$RESULT"
    }
    lsr() { asuser "$LS" "$@"; }
    openapp() { [ -z "$1" ] || asuser "$OPEN" -n "$1"; }   # 以用户身份打开，不带父进程
    KEEP=0
    finish() {    # root 跑的：日志交给用户，暂存的那份由用户自己删，临时目录删掉
      [ -n "$WORK" ] || return 0
      asuser /bin/rm -rf "$STAGED"
      asuser /bin/sh -c 'cat >> "$1"' log "$LOG" < "$WORK/swap.log"
      # 旧版没能放回时留着（那是唯一的一份），改名为 …-keep：下次换包清理没起来的临时目录时不碰它
      # 删之前去掉 uchg / uappnd：带着它们 rm -rf 删不掉，临时目录就一直留着（新版那一步失败时可能还带着）。这里失败不停
      if [ $KEEP = 1 ]; then /bin/mv "$WORK" "$WORK-keep"
      else /usr/bin/find "$WORK" ! -type l -exec /usr/bin/chflags nouchg,nouappnd {} + 2>/dev/null; /bin/rm -rf "$WORK"; fi
    }
    trap finish EXIT
    fail() { say false "$1" "$CUR"; openapp "$CUR"; exit 1; }
    clean() { echo "$1" | tr -d '"\\' | tail -1; }
    echo "== $(/bin/date) swap pid=$PID cur=$CUR target=$TARGET uid=$(/usr/bin/id -u) work=$WORK"
    n=0; while kill -0 "$PID" 2>/dev/null && [ $n -lt 600 ]; do /bin/sleep 0.2; n=$((n+1)); done   # 最多等 2 分钟
    if kill -0 "$PID" 2>/dev/null; then say false "旧版螺壳两分钟还没退出" "$CUR"; exit 1; fi
    NEW="$STAGED"
    if [ -n "$WORK" ]; then
      # 暂存处在用户的目录里，等旧版退出的这会儿可能被换掉：拷进只有 root 能进的临时目录，核过签名，之后只动这一份
      [ -n "$REQ" ] || fail "新版没能准备好（没有核签名的要求）"
      NEW="$WORK/new/$(basename "$TARGET")"
      /bin/mkdir "$WORK/new" "$WORK/old" || fail "新版没能准备好（建不了临时目录）"
      [ ! -L "$STAGED" ] && [ -d "$STAGED" ] && /usr/bin/ditto "$STAGED" "$NEW" || fail "新版没能准备好（拷不过来）"
      # ditto 连 uchg / uappnd 也拷过来：带着它们下面 chown 会失败，装上去的那份也删不掉、换不了。find 不跟链接，同 ACL
      /usr/bin/find "$NEW" ! -type l -exec /usr/bin/chflags nouchg,nouappnd {} + || fail "新版没能准备好（去不掉文件标志）"
      # 换上的那份归 root、组和别人不能写：ditto 留着暂存那份的属主（用户），标准账户就能改「应用程序」里这份，别的账户打开的也是它。
      # 以当前用户跑这条路（只有测试）时 chown 成自己，等于不改
      # ACL 也清掉：ditto（连 --noacl，克隆的文件照样带着）会留着暂存那份上的 ACL，chown、go-w 都不动它，留一条「别人能写」就白改了。
      # 不用 chmod -RN：它跟着符号链接走（-P 也一样），root 会去清包外的文件；find 不跟链接，链接本身没有 ACL
      OWN=0:0; [ $ROOT = 1 ] || OWN="$(/usr/bin/id -u):$(/usr/bin/id -g)"
      /usr/sbin/chown -R "$OWN" "$NEW" && /bin/chmod -R go-w "$NEW" && /usr/bin/find "$NEW" ! -type l -exec /bin/chmod -N {} + || fail "新版没能准备好（改不了属主）"
      /usr/bin/codesign --verify --deep --strict -R "$REQ" "$NEW" || fail "新版的签名不对，没有换。原来的螺壳照常能用"
    fi
    OLD=""; OLDMV=""
    if [ -n "$CUR" ] && [ -e "$CUR" ]; then
      if [ -n "$WORK" ] && [ "$(dirname "$CUR")" = "$(dirname "$TARGET")" ]; then
        OLD="$WORK/old/$(basename "$CUR")"          # 「应用程序」里的旧版：root 挪进自己的临时目录，换成了就删
      else
        OLD="$PREV"; OLDMV=asuser                   # 用户目录里的旧版：以用户身份挪进 previous.noindex
        asuser /bin/mkdir -p "$(dirname "$PREV")"; asuser /bin/rm -rf "$PREV"
      fi
      if ! out=$($OLDMV /bin/mv "$CUR" "$OLD" 2>&1); then fail "搬不动已装的螺壳（$(clean "$out")）"; fi
      [ "$OLD" = "$PREV" ] && lsr -u "$PREV"
    fi
    putback() {   # 旧版放回原处
      [ -n "$OLD" ] || return 0
      if $OLDMV /bin/mv "$OLD" "$CUR"; then lsr -f "$CUR"; return 0; fi
      if [ -n "$WORK" ] && [ "$OLD" != "$PREV" ]; then KEEP=1; echo "旧版没能放回 $CUR，留在 $WORK-keep/old/"; else echo "旧版没能放回 $CUR，留在 $OLD"; fi
    }
    EXTRA=""
    if [ -e "$TARGET" ]; then   # 目标位置上另有一份我们的（两处各装了一份）：先挪开，换成了再删（不是我们的，调用前就拦下了）
      if [ -n "$WORK" ]; then EXTRA="$WORK/extra.app"; lsr -u "$TARGET"; else EXTRA="$PREV.extra"; /bin/rm -rf "$EXTRA"; fi
      if ! out=$(/bin/mv "$TARGET" "$EXTRA" 2>&1); then putback; fail "挪不开「应用程序」里的另一份（$(clean "$out")），旧版已放回"; fi
      [ -n "$WORK" ] || lsr -u "$EXTRA"
    fi
    if ! out=$(/bin/mv "$NEW" "$TARGET" 2>&1); then
      [ -n "$EXTRA" ] && /bin/mv "$EXTRA" "$TARGET"
      putback
      fail "换不上新版（$(clean "$out")），旧版已放回"
    fi
    [ -z "$EXTRA" ] || /bin/rm -rf "$EXTRA"
    lsr -f "$TARGET"
    say true "" "$TARGET"
    openapp "$TARGET"
    echo "== $(/bin/date) done"
    """#

    static func quote(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }

    /// root 那一侧核新版签名的要求：螺壳自己的 Team（取自正在跑的这份的签名，不写在源码里）、Developer ID、同一个 bundle id。
    /// 临时签名的开发版、实验室构建没有 Team，只核标识；正式版读不到自己的 Team 就是 nil（不以 root 换包）
    static func requirement(team: String?, identifier: String, development: Bool) -> String? {
        if let team { return "=" + CodeSigning.requirement(identifier: identifier, team: team) }
        return development ? "=identifier \"\(identifier)\"" : nil
    }

    /// 交给 sh 的那条命令。admin：交给授权框以 root 跑——先清掉以前没起来的换包留下的临时目录（root 的、超过一天的），
    /// 再建只有 root 能进的临时目录，脚本的输出先写在那里，结束时以用户身份追加到用户的日志（root 不往用户能放链接的路径里写）；
    /// 等脚本报「起来了」最多 5 秒，没等到就结束它、把它的输出交给授权框报错、删掉临时目录（退 71：调用方报错，旧版不退出）。
    /// 否则以用户身份跑，输出直接追加到日志。脚本都在后台等我们退出。
    /// 两条路都不用 nohup：macOS 27 上授权框起的 root 进程里 nohup 报 can't detach from console，脚本一行不跑（标准账户因此从没
    /// 升成过）；输入输出都重定向了，没有终端，挂断信号由脚本自己忽略。用户那条路一起改，免得两种写法、只有一种天天有人用
    static func command(admin: Bool, waitPID: pid_t, current: URL?, staged: URL, target: URL, previous: URL, result: URL,
                        uid: uid_t, requirement: String, log: URL, script: String = Swap.script) -> String {
        let head = [String(waitPID), current?.path ?? "", staged.path, target.path, previous.path, result.path, String(uid), requirement]
            .map(quote).joined(separator: " ")
        let run = "/bin/sh -c \(quote(script)) luoke-swap \(head)"
        if admin {
            return [
                "umask 077",
                "/usr/bin/find /private/var/tmp -maxdepth 1 -type d -user 0 -name 'luoke-swap.????????' -mmin +1440 -exec /bin/rm -rf {} + 2>/dev/null",
                "d=$(/usr/bin/mktemp -d /private/var/tmp/luoke-swap.XXXXXXXX) || exit 70",
                "\(run) \"$d\" \(quote(log.path)) </dev/null >\"$d/swap.log\" 2>&1 &",
                "p=$!; i=0",
                "up() { [ -e \"$d/started\" ] || [ ! -d \"$d\" ]; }",     // 已经做完、删了临时目录的也算起来了
                "while ! up && [ $i -lt 50 ]; do /bin/sleep 0.1; i=$((i+1)); done",
                "if ! up; then kill $p 2>/dev/null; /bin/cat \"$d/swap.log\" >&2; /bin/rm -rf \"$d\"; exit 71; fi",
            ].joined(separator: "\n")
        }
        return "\(run) '' \(quote(log.path)) </dev/null >> \(quote(log.path)) 2>&1 &"
    }

    /// 起换包脚本。返回 nil = 起来了（调用方接着退出）；否则是给用户看的原因（例如授权框点了取消），什么都没动。
    static func start(current: URL?, staged: URL, target: URL, waitPID: pid_t, admin: Bool) -> String? {
        let fm = FileManager.default
        try? fm.createDirectory(at: resultFile.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? fm.createDirectory(at: logFile.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? fm.removeItem(at: resultFile)
        try? fm.removeItem(at: oldScriptFile)
        // 要写的目录和日志先以用户身份建好（10-25 核出：root 建出来的，之后用户自己写不进去）
        try? fm.createDirectory(at: Installer.previousDir, withIntermediateDirectories: true)
        if !fm.fileExists(atPath: logFile.path) { fm.createFile(atPath: logFile.path, contents: nil) }
        let bid = Bundle.main.bundleIdentifier ?? "ai.luoke.core"
        guard let req = requirement(team: CodeSigning.selfTeam, identifier: bid, development: BundledApps.developmentBuild) else {
            return "螺壳自己的签名读不到，没有升级。原来的螺壳照常能用。"
        }
        let prev = Installer.previousDir.appendingPathComponent("\(AppInfo.appName).app")
        let shellCmd = command(admin: admin, waitPID: waitPID, current: current, staged: staged, target: target, previous: prev,
                               result: resultFile, uid: getuid(), requirement: req, log: logFile)
        // 换包脚本要改螺壳.app：系统只让同一个开发者的应用改它，disclaim 了的 sh 改不了（Spawn.swift），所以照旧算螺壳
        let p: Process
        if admin {
            // 标准账户：交给系统的管理员授权框（取消了返回 -128）
            func asStr(_ s: String) -> String { "\"" + s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") + "\"" }
            p = Spawn.process("/usr/bin/osascript", ["-e", "do shell script \(asStr(shellCmd)) with prompt \(asStr("螺壳要把新版放进「应用程序」文件夹。")) with administrator privileges"],
                              access: .luoke)
        } else {
            p = Spawn.process("/bin/sh", ["-c", shellCmd], access: .luoke)
        }
        // 授权服务把发起进程的环境交给 root 的 shell，shell 在第一条命令之前就读 SHELLOPTS、PS4 之类（ScanElevate.swift）：只给 PATH
        p.environment = ["PATH": pathValue]
        p.currentDirectoryURL = URL(fileURLWithPath: "/")
        let err = Pipe()
        p.standardError = err
        p.standardOutput = FileHandle.nullDevice
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
