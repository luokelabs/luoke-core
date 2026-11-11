// swift-tools-version:5.9
// 螺壳的本体（V1 §1）：菜单栏状态灯、设置页、首次运行流程。主控的逻辑都在 Python 的 luoke_core 里，
// 这个程序只负责：拉起自带 Python 跑的主控进程、从它的 socket 读状态、把用户的设置写回去。
// 主窗口和 agent 核心并进本体（luoke-core#68）：LUOKE_AGENT_SRC 指钉住的 luoke-agent 时链接它的 LuokeWindow，
// 代码里用 #if canImport(LuokeWindow)；没给（或给的是空）就不带主窗口，本体照旧出。
import PackageDescription

let agentSrc: String? = Context.environment["LUOKE_AGENT_SRC"].flatMap { $0.isEmpty ? nil : $0 }

let package = Package(
    name: "LuokeCore",
    platforms: [.macOS(.v14)],
    dependencies: agentSrc.map { [.package(path: "\($0)/window")] } ?? [],
    targets: [
        .executableTarget(
            name: "LuokeCore",
            dependencies: agentSrc == nil ? [] : [.product(name: "LuokeWindow", package: "window")],
            path: "Sources/LuokeCore"
        ),
        // 本体里能单独测的部分：由本体起的工具（ToolHost.swift）、替扫描弹密码框前的核对（ScanElevate.swift）
        .testTarget(name: "LuokeCoreTests", dependencies: ["LuokeCore"], path: "Tests/LuokeCoreTests"),
    ]
)
