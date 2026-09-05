# JAGENT-2026-P1-11 已提交 OpenAPI 文件不是有效 UTF-8 JSON 文件

## 状态

已修复（2026-08-11 质量门禁回归通过）

## 发现方式

接口契约测试 `tests/test_api_contract_robustness.py`。

## 影响

前端类型生成、OpenAPI 校验器或任何按 UTF-8 读取 `frontend/public/openapi.json` 的工具都会失败，接口契约无法作为可靠的跨前后端输入。

## 复现

```python
import json

with open("frontend/public/openapi.json", encoding="utf-8") as file:
    json.load(file)
```

## 实际结果

读取在字节偏移 `9162` 处失败：`UnicodeDecodeError: 'utf-8' codec can't decode byte 0xc8`。

## 预期结果

该文件应由 `scripts/generate_openapi.py` 生成并以合法 UTF-8 编码保存，可被 Python JSON 解析器和 OpenAPI 工具读取。

## 根因定位

当前工作区的 `frontend/public/openapi.json` 存在非 UTF-8 字节。需要检查生成/合并流程的编码处理，并在契约生成流程中增加 UTF-8 JSON 校验。

## 测试证据

`TestOpenAPIContract.test_openapi_file_is_parseable_and_has_expected_api_surface` 失败。
