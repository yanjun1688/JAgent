"""v8 harness 级不变量测试（I1 终态不可改写）。

只从宪法 / 架构 / contracts 与公开底座（store / world / writer / fold）出发，
不复用 app 层夹具：这一层的缺陷正是“唯一写入口漏了守卫”。
"""
