"""JAgent v8 独立红队测试。

只从规格（docs/jagent-v8-architecture.md 的 I1–I12、contracts、statemachine）出发，
不复用实现者的期望值。发现的缺口用 ``known_bug`` 标记（本目录 conftest 把它转成
strict xfail），生产代码一行不改。
"""
