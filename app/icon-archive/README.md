# 换下来的图标

不打进 螺壳.app（`app/build_app.sh` 只拷 `app/AppIcon.icns` 和 `app/Resources/ui/`），只是留底。

- `2026-10-vermilion/`：朱砂红底的应用图标（`AppIcon.icns`）、窗口里的螺壳图标（`app-core.png`）、安装和升级窗口左栏的黑色海螺（`conch.svg`，按单色模板画）。10-28 换成珊瑚红底、奶油海螺（luoke-core#38，luoke-brand 69df04d）。
- `2026-10-coral/`：珊瑚红底、奶油海螺的应用图标（`AppIcon.icns`）和窗口里的螺壳图标（`app-core.png`）。11-04 换成立体硅胶海螺（beta 2 起，luoke-brand e297db2）：`AppIcon.icns` 用 luoke-brand `media-kit/app-icons/core-1024.png` 按 16–1024 各尺寸缩出、`iconutil` 打成；窗口里的 `app-core.png`、`app-ime.png`、`app-mail.png` 取自 luoke-brand `ui/public/img`；扫描、深度搜索的 `app-scan.png`、`app-search.png` 由 `media-kit/app-icons/` 的 512 缩成 256。
