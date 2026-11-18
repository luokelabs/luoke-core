"""螺壳（luoke-core）的开发版：任务层、派发、引擎监管。只用标准库；打分工作进程用引擎包里的 Python 和 mlx-lm。

规格：docs/V1.md（§2、§8 是冻结的对外约定版本 1）；规则含义：docs/DESIGN.md。
"""

__version__ = "0.2.0"     # 配件清单的 min_core 和它比；0.2.0 起不核配件版本
CONTRACT = 1
