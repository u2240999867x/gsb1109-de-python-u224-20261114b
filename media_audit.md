# media_audit — 按设备档案审计嵌套 @media 资源

归档一批设备说明书时，只接受指定视口宽度和配色模式的资源。本工具读取
CSS 中嵌套的 `@media` 规则，为每项资源给出判定：

- **必用**：条件对这批设备恒真；
- **可能用**：条件只对部分设备成立，或含有未知媒体特性（保留不确定性）；
- **可排除**：条件对这批设备恒假，并给出决定性的条件出处。

## 语义规则

- 逗号分隔的媒体查询是备选分支（任一成立即可）；
- 内外层 `@media` 的条件必须同时成立（AND），判定会沿嵌套链逐层求值；
- 宽度范围区分开闭（`>` / `>=`、`(400px < width <= 800px)` 等），
  与设备视口宽度区间求交；
- 未知媒体特性（如 `hover`、非 px 长度）保留不确定性，不会仅因它
  排除或必用；但若同一分支里已知宽度条件与设备毫无交集，
  该分支仍可确定排除；
- `prefers-color-scheme` 与设备支持的配色模式集合比对；
- `not` 取反已知结论，未知条件取反后仍不确定。

## 用法

```sh
python media_audit.py examples/manuals.css \
    --min-width 360 --max-width 1440 \
    --color-scheme light,dark --media-type screen
```

- `--min-width` / `--max-width`：这批设备的视口宽度范围（px，可只给一端）；
- `--color-scheme`：设备支持的配色模式，逗号分隔（默认 `light,dark`）；
- `--media-type`：设备媒体类型，逗号分隔（默认 `screen`）；
- `--json`：以 JSON 输出判定结果。

报告中每项资源附“依据”，标明决定性条件所在的层级（第 N 层）与
`@media` 原文，作为判定出处。

## 测试

```sh
python -m pytest tests/test_media_audit.py
```
