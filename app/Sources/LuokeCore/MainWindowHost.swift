import AppKit

/// 主窗口的最小尺寸：内容 860×600（luoke-agent 的窗口按它画）。不能只设 contentMinSize：NSHostingView 一布局就按
/// SwiftUI 内容的最小尺寸改写它，主窗口的内容除侧栏外都能缩到 0，窗口就能拉到约 240 宽（core-private#230）。
/// 所以拖的时候在 windowWillResize 里卡住；内容要得更多（右侧面板开着）时，AppKit 照内容的更大的最小尺寸
enum MainWindowHost {
    static let minSize = NSSize(width: 860, height: 600)

    /// 拖动边缘时给的新尺寸（窗口外框）不小于最小内容尺寸对应的外框
    static func clamp(_ frameSize: NSSize, in w: NSWindow) -> NSSize {
        let floor = w.frameRect(forContentRect: NSRect(origin: .zero, size: minSize)).size
        return NSSize(width: max(frameSize.width, floor.width), height: max(frameSize.height, floor.height))
    }

    /// 存下来的位置（setFrameAutosaveName 恢复的）可能是修好之前拉窄的：打开时撑回最小尺寸
    static func enforce(on w: NSWindow) {
        let size = clamp(w.frame.size, in: w)
        guard size != w.frame.size else { return }
        var f = w.frame
        f.origin.y -= size.height - f.height          // 左上角不动
        f.size = size
        w.setFrame(f, display: false)
    }
}
