"""主控 DMG 的窗口（luoke-core#30，ADR 0012）：左边「螺壳」、右边「应用程序」的快捷方式，中间 brand 的箭头背景，拖过去就装好。
用内置的 dmgbuild（tools/vendor，MIT）直接写 Finder 的 .DS_Store，不驱动 Finder，出版机器的 launchd 会话里也能跑。

    python3 -B tools/dmg/layout.py <某.app> <输出.dmg> <卷名>

背景图来自 luokelabs/luoke-brand ui/dmg（742c5ba）：660×400，@2x 一并读进去。图标 128，app 在 (165,200)，「应用程序」在 (495,200)。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "vendor"))
import dmgbuild  # noqa: E402


def main() -> None:
    app, out, volname = sys.argv[1], sys.argv[2], sys.argv[3]
    name = os.path.basename(app.rstrip("/"))
    settings = {
        "format": "UDZO",
        "filesystem": "HFS+",
        "files": [app],
        "symlinks": {"Applications": "/Applications"},
        "icon_locations": {name: (165, 200), "Applications": (495, 200)},
        "background": os.path.join(HERE, "background.png"),
        "window_rect": ((200, 120), (660, 400)),
        "icon_size": 128,
        "text_size": 13,
        "show_status_bar": False, "show_tab_view": False, "show_toolbar": False, "show_pathbar": False, "show_sidebar": False,
        "default_view": "icon-view",
        "show_icon_preview": False,
        "include_icon_view_settings": True,
        "arrange_by": None,
    }
    dmgbuild.build_dmg(out, volname, settings=settings)


if __name__ == "__main__":
    main()
