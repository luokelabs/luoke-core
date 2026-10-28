// swift-tools-version:5.9
// 螺壳的本体（V1 §1）：菜单栏状态灯、设置页、首次运行流程。主控的逻辑都在 Python 的 luoke_core 里，
// 这个程序只负责：拉起自带 Python 跑的主控进程、从它的 socket 读状态、把用户的设置写回去。
import PackageDescription

let package = Package(
    name: "LuokeCore",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "LuokeCore",
            path: "Sources/LuokeCore"
        )
    ]
)
